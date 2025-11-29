"""
Smart Tick Scalper V2 (修复版 - 显式报错)
此版本修复了错误日志被吞没的问题，并强制策略高频循环。
"""
from __future__ import annotations
import time
from typing import Dict, Any, Optional
from strategies.market_maker import MarketMaker, format_balance
from utils.helpers import round_to_tick_size, round_to_precision
from logger import setup_logger

logger = setup_logger("tick_scalper_v2")

class SmartTickScalper(MarketMaker):
    def __init__(self, *args, **kwargs):
        # 提取 market_type 参数 (如果有)
        self.market_type = kwargs.pop('market_type', 'spot')
        
        # --- 强制覆盖配置 ---
        kwargs['max_orders'] = 1             # 单次只做一个订单
        kwargs['enable_rebalance'] = False   # 禁用外部重平逻辑
        
        # 关键：强制父类不要等待成交，每次都进入策略判断
        kwargs['wait_all_filled'] = False 
        
        super().__init__(*args, **kwargs)
        
        # --- 策略状态 ---
        self.state = "IDLE"  # IDLE (空仓), BUYING (挂买中), SELLING (挂卖中)
        
        # --- 持仓数据 ---
        self.held_quantity = 0.0
        self.avg_cost = 0.0
        self.hold_start_time = 0
        
        # --- 核心参数 (可在代码中调整) ---
        self.balance_pct = 0.95        # 资金利用率
        self.max_hold_seconds = 60     # 持仓超时止损
        self.stop_loss_pct = 0.005     # 价格止损幅度
        self.chase_bid = True          # 开启买单追价
        self.chase_ask = True          # 开启卖单追价
        
        # 强制设置一个很大的价差阈值，防止父类逻辑干扰，完全由本策略接管
        self.force_adjust_spread = 0.0 
        
        logger.info(f"Smart Tick Scalper V2 (修复版) 已启动 [{self.market_type.upper()}]")

    def _price_deviation_exceeds_spread(self, current_price: float) -> bool:
        """
        [关键修复] 强制返回 True，欺骗 run.py 的主循环，
        让它每一轮 interval 都调用 place_limit_orders。
        """
        return True

    def get_actual_position(self) -> float:
        """获取真实净持仓"""
        if self.market_type == 'perp':
            try:
                positions = self.client.get_positions(self.symbol)
                if not positions or (isinstance(positions, dict) and 'error' in positions):
                    return 0.0
                if isinstance(positions, list):
                    for pos in positions:
                        if pos.get('symbol') == self.symbol:
                            return float(pos.get('netQuantity') or pos.get('size') or 0.0)
                return 0.0
            except Exception as e:
                logger.error(f"查询合约持仓失败: {e}")
                return 0.0
        else:
            # 现货：读取钱包余额
            available, total = self.get_asset_balance(self.base_asset)
            # 如果钱包里的币少于最小下单量，视为无持仓
            if total < self.min_order_size:
                return 0.0
            return total

    def place_limit_orders(self):
        """策略主循环"""
        # 1. 连接检查
        if not self.check_ws_connection():
            return
        
        # 2. 获取盘口
        bid_price, ask_price = self.get_market_depth()
        if not bid_price or not ask_price:
            logger.warning("等待盘口数据...")
            return

        # 3. 获取持仓
        net = self.get_actual_position()
        
        # 4. 状态机逻辑
        if net > self.min_order_size:
            # 有持仓 -> 强制进入卖出流程
            if self.state != "SELLING":
                logger.info(f"检测到持仓 {net}，切换到 [SELLING] 模式")
                self.held_quantity = net
                self.state = "SELLING"
                if self.avg_cost == 0: 
                    self.avg_cost = bid_price # 丢失成本时，以当前买价作为估算
                    self.hold_start_time = time.time()
            
            self._execute_sell_logic(bid_price, ask_price)
            
        else:
            # 无持仓 -> 买入流程
            self.state = "BUYING"
            self.held_quantity = 0
            self._execute_buy_logic(bid_price, ask_price)

    def _execute_buy_logic(self, best_bid: float, best_ask: float):
        """执行买入逻辑"""
        # 1. 检查是否需要追单
        if self.active_buy_orders:
            current_order = self.active_buy_orders[0]
            current_price = float(current_order['price'])
            
            # 如果开启追单，且 市场买一 > 我的挂单
            if self.chase_bid and best_bid > current_price:
                # 风控：只有价差正常时才追
                if (best_ask - best_bid) > 0: 
                    logger.info(f"🚀 追单: 市场 {best_bid} > 挂单 {current_price}，撤单重挂")
                    self.cancel_existing_orders()
                else:
                    logger.debug("Spread 过小或倒挂，暂不追单")
            return

        # 2. 计算下单数量
        quote_available, _ = self.get_asset_balance(self.quote_asset)
        
        # 只有在还没挂单的时候才检查余额日志，防止刷屏
        if not self.active_buy_orders:
            # 每10秒打印一次余额，方便调试
            if int(time.time()) % 10 == 0:
                logger.info(f"准备买入: 可用余额 {quote_available:.2f} {self.quote_asset}")

        target_quote_amount = quote_available * self.balance_pct
        
        # 计算数量
        quantity = target_quote_amount / best_bid
        quantity = round_to_precision(quantity, self.base_precision)
        
        # 必须大于最小下单量
        if quantity < self.min_order_size:
            if not self.active_buy_orders and int(time.time()) % 10 == 0:
                logger.warning(f"❌ 资金不足以购买最小单位: 需要 {self.min_order_size} {self.base_asset}, 计算得出 {quantity}")
            return

        # 双重检查防止资金不足错误
        if quantity * best_bid > quote_available:
            quantity = round_to_precision(quantity * 0.99, self.base_precision)
            
        # 3. 挂单价格：挂 Best Bid
        price = best_bid
        self._place_post_only_order("Bid", price, quantity)

    def _execute_sell_logic(self, best_bid: float, best_ask: float):
        """执行卖出逻辑"""
        if self.held_quantity < self.min_order_size:
            return

        # 初始化时间
        if self.hold_start_time == 0:
            self.hold_start_time = time.time()

        hold_duration = time.time() - self.hold_start_time
        if self.avg_cost == 0: self.avg_cost = best_bid
        
        unrealized_pnl_pct = (best_bid - self.avg_cost) / self.avg_cost

        is_stop_loss = False
        target_price = 0.0

        # === 决策 ===
        if hold_duration > self.max_hold_seconds or unrealized_pnl_pct < -self.stop_loss_pct:
            is_stop_loss = True
            target_price = best_ask # 止损：挂卖一尽快跑
            if int(time.time()) % 5 == 0:
                logger.warning(f"⚠️ 触发 Maker 止损 (持仓 {hold_duration:.0f}s, 盈亏 {unrealized_pnl_pct*100:.2f}%)")
        else:
            target_price = best_ask # 正常：挂卖一排队
            min_profit_price = self.avg_cost + self.tick_size
            if target_price < min_profit_price:
                target_price = min_profit_price

        # 检查当前挂单
        if self.active_sell_orders:
            current_order = self.active_sell_orders[0]
            current_price = float(current_order['price'])
            
            # 止损追跌
            if is_stop_loss and self.chase_ask and best_ask < current_price:
                logger.info(f"📉 止损追价: 市场 {best_ask} < 挂单 {current_price}，撤单")
                self.cancel_existing_orders()
                return

            if abs(current_price - target_price) >= self.tick_size:
                 if (is_stop_loss and target_price < current_price) or (not is_stop_loss and target_price > current_price):
                     self.cancel_existing_orders()
            return

        final_price = max(target_price, best_bid + self.tick_size)
        self._place_post_only_order("Ask", final_price, self.held_quantity)

    def _place_post_only_order(self, side: str, price: float, quantity: float):
        """发送 PostOnly 订单 (带详细错误处理)"""
        price = round_to_tick_size(price, self.tick_size)
        quantity = round_to_precision(quantity, self.base_precision)
        
        order = {
            "orderType": "Limit",
            "price": str(price),
            "quantity": str(quantity),
            "side": side,
            "symbol": self.symbol,
            "postOnly": True,
            "timeInForce": "GTC"
        }
        
        if self.exchange == "backpack":
            order["autoLendRedeem"] = True
            
        res = self.client.execute_order(order)
        
        if isinstance(res, dict) and "error" in res:
            err_msg = str(res['error'])
            
            # [关键修复] 只忽略 PostOnly 错误，其他错误全部打印！
            if "post" in err_msg.lower() or "maker" in err_msg.lower():
                logger.debug(f"PostOnly 触发 (价格 {price} 已穿过盘口)，等待下一轮")
            else:
                logger.error(f"❌ 下单失败 [{side} {quantity}@{price}]: {err_msg}")
        else:
            logger.info(f"✅ 挂单成功: {side} {quantity} @ {price}")
            if side == "Bid":
                self.active_buy_orders.append(res)
            else:
                self.active_sell_orders.append(res)

    def _after_fill_processed(self, fill_info: Dict[str, Any]) -> None:
        """成交后回调"""
        super()._after_fill_processed(fill_info)
        
        side = fill_info.get("side")
        quantity = float(fill_info.get("quantity", 0))
        price = float(fill_info.get("price", 0))
        
        if quantity < self.min_order_size * 0.1: return

        if side == "Bid":
            logger.info(f"⚡ 买入成交 {quantity} @ {price} -> 切换至 [SELLING]")
            self.state = "SELLING"
            self.held_quantity = quantity
            self.avg_cost = price
            self.hold_start_time = time.time()
            self.cancel_existing_orders()
            
        elif side == "Ask":
            profit = (price - self.avg_cost) * quantity
            logger.info(f"💰 卖出成交 {quantity} @ {price} (盈亏: {profit:.4f}) -> 切换至 [BUYING]")
            self.state = "IDLE" 
            self.held_quantity = 0
            self.cancel_existing_orders()
