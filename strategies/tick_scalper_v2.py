"""
Smart Tick Scalper V2 (现货/合约 通用版)
策略核心：
1. 极速 Maker：始终挂买一价买入，挂卖一价卖出。
2. 零 Taker：严禁市价单，止损也用 Limit 单排队。
3. 动态追单：防止踏空上涨，防止下跌被深套。
4. 通用支持：自动识别现货或合约，读取真实持仓。
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
        
        super().__init__(*args, **kwargs)
        
        # --- 策略状态 ---
        self.state = "IDLE"  # IDLE (空仓), BUYING (挂买中), SELLING (挂卖中)
        
        # --- 持仓数据 ---
        self.held_quantity = 0.0
        self.avg_cost = 0.0
        self.hold_start_time = 0
        
        # --- 核心参数 (可在代码中调整) ---
        self.balance_pct = 0.90        # 每次使用 90% 的可用 U 下单
        self.max_hold_seconds = 45     # 持仓超过 45 秒未卖出，触发强制 Maker 止损
        self.stop_loss_pct = 0.004     # 亏损超过 0.4% 触发 Maker 止损
        self.chase_bid = True          # 开启买单追价
        self.chase_ask = True          # 开启卖单追价
        
        logger.info(f"Smart Tick Scalper V2 已启动 [{self.market_type.upper()}]")
        logger.info(f"资金使用率: {self.balance_pct*100}% | 超时止损: {self.max_hold_seconds}s")

    def get_actual_position(self) -> float:
        """获取真实净持仓 (兼容 现货/合约)"""
        # 1. 如果是合约，强制从 API 读取
        if self.market_type == 'perp':
            try:
                positions = self.client.get_positions(self.symbol)
                # 处理 Backpack 返回空列表或错误的情况
                if not positions or (isinstance(positions, dict) and 'error' in positions):
                    return 0.0
                
                # 找到当前 Symbol 的持仓
                if isinstance(positions, list):
                    for pos in positions:
                        if pos.get('symbol') == self.symbol:
                            # 兼容不同字段名 netQuantity / size
                            qty = float(pos.get('netQuantity') or pos.get('size') or 0.0)
                            return qty
                return 0.0
            except Exception as e:
                logger.error(f"查询合约持仓失败: {e}")
                return 0.0
        
        # 2. 如果是现货，使用基类的内存计数 (total_bought - total_sold)
        # 或者使用 get_asset_balance 读取钱包余额 (更准确)
        else:
            # 尝试直接读取钱包 Base Asset (如 SOL) 的可用余额
            # 注意：这假设你账户里的 SOL 都是用来跑策略的
            available, total = self.get_asset_balance(self.base_asset)
            # 如果内存记录偏差太大，以钱包余额为准
            net_memory = self.get_net_position()
            
            # 只有当钱包余额 > 最小下单量时，才认为有持仓
            if total > self.min_order_size:
                return total
            return net_memory

    def place_limit_orders(self):
        """策略主循环"""
        self.check_ws_connection()
        
        # 1. 获取最新盘口
        bid_price, ask_price = self.get_market_depth()
        if not bid_price or not ask_price:
            return

        # 2. 状态机流转
        
        # 场景 A: 刚启动或空仓
        if self.state == "IDLE":
            # 获取真实持仓
            net = self.get_actual_position()
            
            # 如果持仓 > 最小下单量，说明有遗留仓位，直接进入卖出模式
            if net > self.min_order_size:
                logger.info(f"检测到持仓 {net}，切换到 [SELLING] 模式")
                self.held_quantity = net
                self.avg_cost = self._calculate_average_buy_cost()
                if self.avg_cost == 0: self.avg_cost = bid_price * 0.999 # 估算成本
                self.hold_start_time = time.time()
                self.state = "SELLING"
                self._execute_sell_logic(bid_price, ask_price)
            else:
                self.state = "BUYING"
                self._execute_buy_logic(bid_price, ask_price)

        # 场景 B: 正在买入
        elif self.state == "BUYING":
            self._execute_buy_logic(bid_price, ask_price)

        # 场景 C: 正在卖出
        elif self.state == "SELLING":
            self._execute_sell_logic(bid_price, ask_price)

    def _execute_buy_logic(self, best_bid: float, best_ask: float):
        """执行买入逻辑"""
        # 1. 检查追单
        if self.active_buy_orders:
            current_order = self.active_buy_orders[0]
            current_price = float(current_order['price'])
            
            if self.chase_bid and best_bid > current_price:
                if (best_ask - best_bid) >= self.tick_size: 
                    logger.info(f"🚀 追单: 市场买一 {best_bid} > 挂单 {current_price}，撤单重挂")
                    self.cancel_existing_orders()
            return

        # 2. 计算下单数量
        quote_available, _ = self.get_asset_balance(self.quote_asset)
        target_quote_amount = quote_available * self.balance_pct
        
        if target_quote_amount < 1.0:
             if len(self.active_buy_orders) == 0:
                 # 日志节流
                 if int(time.time()) % 10 == 0:
                    logger.warning(f"余额不足: {quote_available} {self.quote_asset}")
             return

        quantity = target_quote_amount / best_bid
        quantity = round_to_precision(quantity, self.base_precision)
        quantity = max(self.min_order_size, quantity)
        
        if quantity * best_bid > quote_available:
            quantity = round_to_precision(quantity * 0.99, self.base_precision)
            
        price = best_bid
        self._place_post_only_order("Bid", price, quantity)

    def _execute_sell_logic(self, best_bid: float, best_ask: float):
        """执行卖出逻辑 (含 Maker 止损)"""
        # 二次确认持仓 (防止卖飞或卖空)
        if self.market_type == 'perp':
             # 合约模式下，如果仓位没了，立即停止
             current_pos = self.get_actual_position()
             if current_pos < self.min_order_size:
                 logger.info("合约仓位已平，重置为 IDLE")
                 self.state = "IDLE"
                 self.cancel_existing_orders()
                 return
        
        if self.held_quantity < self.min_order_size:
            self.state = "IDLE"
            self.cancel_existing_orders()
            return

        hold_duration = time.time() - self.hold_start_time
        unrealized_pnl_pct = (best_bid - self.avg_cost) / self.avg_cost

        is_stop_loss = False
        target_price = 0.0

        # === 决策 A: 止损模式 ===
        if hold_duration > self.max_hold_seconds or unrealized_pnl_pct < -self.stop_loss_pct:
            is_stop_loss = True
            target_price = best_ask
            if int(time.time()) % 5 == 0:
                logger.warning(f"⚠️ 触发 Maker 止损 (持仓 {hold_duration:.0f}s, 盈亏 {unrealized_pnl_pct*100:.2f}%)，目标 {target_price}")
        
        # === 决策 B: 止盈/正常模式 ===
        else:
            target_price = best_ask
            min_profit_price = self.avg_cost + self.tick_size
            if target_price < min_profit_price:
                target_price = min_profit_price

        # 检查当前挂单
        if self.active_sell_orders:
            current_order = self.active_sell_orders[0]
            current_price = float(current_order['price'])
            
            if is_stop_loss and self.chase_ask and best_ask < current_price:
                logger.info(f"📉 止损追价: 市场卖一 {best_ask} < 挂单 {current_price}，撤单")
                self.cancel_existing_orders()
                return

            if abs(current_price - target_price) >= self.tick_size:
                 if (is_stop_loss and target_price < current_price) or (not is_stop_loss and target_price > current_price):
                     self.cancel_existing_orders()
            return

        final_price = max(target_price, best_bid + self.tick_size)
        self._place_post_only_order("Ask", final_price, self.held_quantity)

    def _place_post_only_order(self, side: str, price: float, quantity: float):
        """发送 PostOnly 订单"""
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
            pass # 忽略 PostOnly 错误
        else:
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
            logger.info(f"✅ 买入成交 {quantity} @ {price} -> 切换至 [SELLING]")
            self.state = "SELLING"
            self.held_quantity = quantity
            self.avg_cost = price
            self.hold_start_time = time.time()
            self.cancel_existing_orders()
            
        elif side == "Ask":
            profit = (price - self.avg_cost) * quantity
            logger.info(f"💰 卖出成交 {quantity} @ {price} (盈亏: {profit:.4f} U) -> 切换至 [BUYING]")
            self.state = "IDLE"
            self.held_quantity = 0
            self.cancel_existing_orders()
