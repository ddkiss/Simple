"""
Smart Tick Scalper V3.1 (分级止损版)
特性更新:
1. [分级止损] 
   - 超时止损 -> 挂 Maker 单 (Best Ask)，省手续费
   - 价格止损 -> 打 Taker 单 (Best Bid)，保命优先
2. [API优化] 最小追单阈值防止频繁挂单
3. [资金优化] 资金利用率可配置
4. [冷却风控] 亏损后暂停交易
"""
from __future__ import annotations
import time
from typing import Dict, Any, Optional
from strategies.market_maker import MarketMaker, format_balance
from utils.helpers import round_to_tick_size, round_to_precision
from logger import setup_logger

logger = setup_logger("tick_scalper_v3_1")

class SmartTickScalper(MarketMaker):
    def __init__(self, *args, **kwargs):
        # 提取 market_type 参数 (如果有)
        self.market_type = kwargs.pop('market_type', 'spot')
        
        # --- 强制覆盖配置 ---
        kwargs['max_orders'] = 1             # 单次只做一个订单
        kwargs['enable_rebalance'] = False   # 禁用外部重平逻辑
        kwargs['wait_all_filled'] = False    # 强制不等待，由策略控制循环
        
        super().__init__(*args, **kwargs)
        
        # --- 策略状态 ---
        self.state = "IDLE"  # IDLE (空仓), BUYING (挂买中), SELLING (挂卖中)
        
        # --- 持仓数据 ---
        self.held_quantity = 0.0
        self.avg_cost = 0.0
        self.hold_start_time = 0
        
        # --- [风控] 止损冷却 ---
        self.last_stop_loss_time = 0
        self.stop_loss_cooldown = kwargs.get('stop_loss_cooldown', 65)
        
        # --- [优化] 资金利用率 ---
        self.balance_pct = kwargs.get('balance_pct', 0.92)
        
        # --- 核心参数 ---
        self.max_hold_seconds = 145     # 持仓超时止损
        self.stop_loss_pct = 0.01      # 价格止损幅度
        self.chase_bid = True           # 开启买单追价
        self.chase_ask = True           # 开启卖单追价
        
        # --- [优化] 最小追单阈值 ---
        self.min_chase_pct = kwargs.get('min_chase_pct', 0.00001)
        
        # 强制设置一个很大的价差阈值，防止父类逻辑干扰
        self.force_adjust_spread = 10 
        
        logger.info(f"Smart Tick Scalper V3.1 (分级止损) 已启动 [{self.market_type.upper()}]")
        logger.info(f"配置: 止损={self.stop_loss_pct*100}%, 超时={self.max_hold_seconds}s")
        logger.info(f"逻辑: 价格止损->Taker, 超时止损->Maker")

    def _price_deviation_exceeds_spread(self, current_price: float) -> bool:
        """强制返回 True，保持高频循环"""
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
            _, total = self.get_asset_balance(self.base_asset)
            if total < self.min_order_size:
                return 0.0
            return total

    def place_limit_orders(self):
        """策略主循环"""
        
        # 0. [风控] 冷却期检查
        if self.last_stop_loss_time > 0:
            elapsed = time.time() - self.last_stop_loss_time
            if elapsed < self.stop_loss_cooldown:
                if int(time.time()) % 10 == 0:
                    logger.info(f"🧊 冷却中... 暂停交易 (剩余 {self.stop_loss_cooldown - elapsed:.0f}s)")
                if self.active_buy_orders:
                    self.cancel_existing_orders()
                return

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
            # 有持仓 -> 卖出流程
            if self.state != "SELLING":
                logger.info(f"检测到持仓 {net}，切换到 [SELLING] 模式")
                self.held_quantity = net
                self.state = "SELLING"
                if self.avg_cost == 0: 
                    self.avg_cost = bid_price # 丢失成本时兜底
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
            
            diff_pct = abs(best_bid - current_price) / current_price
            
            if self.chase_bid and best_bid > current_price:
                # [API优化] 超过阈值才追
                if diff_pct > self.min_chase_pct:
                    if (best_ask - best_bid) > 0: 
                        logger.info(f"🚀 追单: 市场 {best_bid} > 挂单 {current_price} (偏离 {diff_pct:.4%})")
                        self.cancel_existing_orders()
                    else:
                        logger.debug("Spread 倒挂，暂不追单")
            return

        # 2. 计算下单数量
        quote_available, _ = self.get_asset_balance(self.quote_asset)
        
        if not self.active_buy_orders and int(time.time()) % 10 == 0:
            logger.info(f"准备买入: 可用余额 {quote_available:.2f} {self.quote_asset}")

        # [资金优化]
        target_quote_amount = quote_available * self.balance_pct
        
        quantity = target_quote_amount / best_bid
        quantity = round_to_precision(quantity, self.base_precision)
        
        if quantity < self.min_order_size:
            if not self.active_buy_orders and int(time.time()) % 10 == 0:
                logger.warning(f"❌ 资金不足以购买最小单位: 需要 {self.min_order_size} {self.base_asset}")
            return

        if quantity * best_bid > quote_available:
            quantity = round_to_precision(quantity * 0.99, self.base_precision)
            
        # 3. 挂单价格：挂 Best Bid (Maker)
        self._place_order_safe("Bid", best_bid, quantity, post_only=True)

    def _execute_sell_logic(self, best_bid: float, best_ask: float):
        """执行卖出逻辑 (分级止损)"""
        if self.held_quantity < self.min_order_size:
            return

        if self.hold_start_time == 0:
            self.hold_start_time = time.time()

        hold_duration = time.time() - self.hold_start_time
        if self.avg_cost == 0: self.avg_cost = best_bid
        
        # 浮动盈亏比例
        unrealized_pnl_pct = (best_bid - self.avg_cost) / self.avg_cost

        # === 核心决策逻辑 ===
        target_price = 0.0
        use_post_only = True  # 默认为 Maker
        scenario = "NORMAL"

        if unrealized_pnl_pct < -self.stop_loss_pct:
            # 场景A: 价格触发止损 -> 紧急 Taker 离场
            scenario = "STOP_LOSS_PRICE"
            target_price = best_bid  # 砸给买一
            use_post_only = False    # 允许 Taker
            if int(time.time()) % 5 == 0:
                logger.warning(f"🚨 触发价格止损 (盈亏 {unrealized_pnl_pct*100:.2f}%) -> Taker 离场")

        elif hold_duration > self.max_hold_seconds:
            # 场景B: 超时止损 -> 挂 Maker 离场 (用户需求)
            scenario = "STOP_LOSS_TIMEOUT"
            target_price = best_ask  # 挂在卖一排队
            use_post_only = True     # 强制 Maker
            if int(time.time()) % 5 == 0:
                logger.warning(f"⏰ 触发超时止损 (持仓 {hold_duration:.0f}s) -> Maker 排队")
        
        else:
            # 场景C: 正常止盈/持有
            scenario = "PROFIT"
            target_price = best_ask
            min_profit_price = self.avg_cost + self.tick_size
            if target_price < min_profit_price:
                target_price = min_profit_price
            use_post_only = True

        # === 订单执行与调整 ===
        
        if self.active_sell_orders:
            current_order = self.active_sell_orders[0]
            current_price = float(current_order['price'])
            
            # 1. 价格止损的特殊追单逻辑 (Taker)
            if scenario == "STOP_LOSS_PRICE":
                # 如果当前挂单价格比市场买一还高（卖不掉），或者为了确保成交
                # Taker 模式下，如果买一价变了，我们应该撤单重打新的买一价
                if current_price != best_bid:
                     logger.info(f"📉 价格止损追单: 改挂 {best_bid} (Taker)")
                     self.cancel_existing_orders()
                return

            # 2. 超时止损的追单逻辑 (Maker)
            if scenario == "STOP_LOSS_TIMEOUT":
                # 我们挂在 Ask，如果 Ask 跑远了，我们要跟过去
                # 如果 Ask 变低了（行情下跌），我们也得降价挂新的 Ask
                price_diff = abs(current_price - target_price)
                if price_diff >= self.tick_size:
                    logger.info(f"🔄 超时订单调整: 跟随卖一 {target_price}")
                    self.cancel_existing_orders()
                return

            # 3. 正常模式的调整逻辑 (API优化)
            price_diff = abs(current_price - target_price)
            diff_pct = price_diff / current_price
            
            if price_diff >= self.tick_size and diff_pct > self.min_chase_pct:
                 logger.info(f"🔄 正常订单调整: {target_price}")
                 self.cancel_existing_orders()
            return

        # 无挂单，发送新订单
        # 只有正常模式下，才需要防止“卖价低于买价”的倒挂保护（Taker止损不需要，就是要砸）
        if use_post_only:
            final_price = max(target_price, best_bid + self.tick_size)
        else:
            final_price = target_price # Taker 模式直接用目标价

        self._place_order_safe("Ask", final_price, self.held_quantity, post_only=use_post_only)

    def _place_order_safe(self, side: str, price: float, quantity: float, post_only: bool = True):
        """发送订单通用封装"""
        price = round_to_tick_size(price, self.tick_size)
        quantity = round_to_precision(quantity, self.base_precision)
        
        order = {
            "orderType": "Limit",
            "price": str(price),
            "quantity": str(quantity),
            "side": side,
            "symbol": self.symbol,
            "postOnly": post_only,
            "timeInForce": "GTC"
        }
        
        if self.exchange == "backpack":
            order["autoLendRedeem"] = True
            
        res = self.client.execute_order(order)
        
        if isinstance(res, dict) and "error" in res:
            err_msg = str(res['error'])
            # 只有在强制 Maker 且被拒单时才忽略错误
            if post_only and ("post" in err_msg.lower() or "maker" in err_msg.lower()):
                logger.debug(f"PostOnly 触发 (价格 {price})，等待下一轮")
            else:
                logger.error(f"❌ 下单失败 [{side} {quantity}@{price} PostOnly={post_only}]: {err_msg}")
        else:
            type_str = "Maker" if post_only else "Taker"
            logger.info(f"✅ 挂单成功 [{type_str}]: {side} {quantity} @ {price}")
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
            
            # [风控] 亏损触发冷却
            if profit < 0:
                logger.warning(f"🛑 亏损离场 (PnL: {profit:.4f})，冷却 {self.stop_loss_cooldown}秒...")
                self.last_stop_loss_time = time.time()

            self.state = "IDLE" 
            self.held_quantity = 0
            self.avg_cost = 0
            self.cancel_existing_orders()
