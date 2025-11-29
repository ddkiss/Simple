"""
Smart Tick Scalper V2 (VIP5 专用极速刷量版)
策略核心：
1. 极速 Maker：始终挂买一价买入，挂卖一价卖出。
2. 零 Taker：严禁市价单，止损也用 Limit 单排队。
3. 动态追单：防止踏空上涨，防止下跌被深套。
4. 资金管理：自动使用 90% 可用余额，复利滚仓。
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
        self.balance_pct = 0.80        # 每次使用 90% 的可用 U 下单 (保留 10% 缓冲)
        self.max_hold_seconds = 120     # 持仓超过 45 秒未卖出，触发强制 Maker 止损
        self.stop_loss_pct = 0.008     # 亏损超过 0.4% 触发 Maker 止损
        self.chase_bid = True          # 开启买单追价 (防止踏空)
        self.chase_ask = True          # 开启卖单追价 (止损时防止套牢)
        
        logger.info(f"Smart Tick Scalper V2 已启动 | 资金使用率: {self.balance_pct*100}% | 超时止损: {self.max_hold_seconds}s")

    def place_limit_orders(self):
        """策略主循环：由 run.py 定时调用"""
        self.check_ws_connection()
        
        # 1. 获取最新盘口 (极速模式依赖 WS 推送的本地 Orderbook 可能会更快，这里用通用接口)
        bid_price, ask_price = self.get_market_depth()
        if not bid_price or not ask_price:
            return

        # 2. 状态机流转
        # 场景 A: 刚启动或空仓
        if self.state == "IDLE":
            # 检查是否有遗留持仓 (防止程序重启后不知道要卖)
            net = self.get_net_position()
            if net > self.min_order_size:
                logger.info(f"检测到遗留持仓 {net}，切换到 [SELLING] 模式")
                self.held_quantity = net
                self.avg_cost = self._calculate_average_buy_cost()
                if self.avg_cost == 0: self.avg_cost = bid_price * 0.99 # 无法获取成本时保守估算
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
        
        # 1. 检查当前挂单状态 (追单逻辑)
        if self.active_buy_orders:
            current_order = self.active_buy_orders[0]
            current_price = float(current_order['price'])
            
            # 如果开启追单，且市场买一价已经超过我的挂单价
            if self.chase_bid and best_bid > current_price:
                # 风控：如果 Spread 极小 (例如 1 tick)，说明可能在剧烈波动，稍微等等
                # 但为了刷量，通常只要 Spread > 0 就追
                if (best_ask - best_bid) > self.tick_size: 
                    logger.info(f"🚀 追单: 市场买一 {best_bid} > 挂单 {current_price}，撤单重挂")
                    self.cancel_existing_orders()
                else:
                    logger.debug("Spread 过小，暂不追单")
            return

        # 2. 计算下单数量 (自动 90% 仓位)
        quote_available, _ = self.get_asset_balance(self.quote_asset)
        target_quote_amount = quote_available * self.balance_pct
        
        if target_quote_amount < 1.0: # 余额过少保护
             if len(self.active_buy_orders) == 0:
                 logger.warning(f"余额不足: {quote_available} {self.quote_asset}")
             return

        quantity = target_quote_amount / best_bid
        quantity = round_to_precision(quantity, self.base_precision)
        quantity = max(self.min_order_size, quantity)
        
        # 双重检查防止资金不足错误
        if quantity * best_bid > quote_available:
            quantity = round_to_precision(quantity * 0.99, self.base_precision)
            
        # 3. 挂单价格：永远挂 Best Bid (Maker)
        price = best_bid
        
        self._place_post_only_order("Bid", price, quantity)

    def _execute_sell_logic(self, best_bid: float, best_ask: float):
        """执行卖出逻辑 (含 Maker 止损)"""
        if self.held_quantity < self.min_order_size:
            logger.warning("状态 SELLING 但持仓不足，重置 IDLE")
            self.state = "IDLE"
            self.cancel_existing_orders()
            return

        # 计算持仓数据
        hold_duration = time.time() - self.hold_start_time
        unrealized_pnl_pct = (best_bid - self.avg_cost) / self.avg_cost

        is_stop_loss = False
        target_price = 0.0

        # === 决策 A: 止损模式 ===
        if hold_duration > self.max_hold_seconds or unrealized_pnl_pct < -self.stop_loss_pct:
            is_stop_loss = True
            # Maker 止损核心：挂卖一价 (Best Ask) 尽快离场
            target_price = best_ask
            
            # 日志节流
            if int(time.time()) % 5 == 0:
                logger.warning(f"⚠️ 触发 Maker 止损 (持仓 {hold_duration:.0f}s, 盈亏 {unrealized_pnl_pct*100:.2f}%)，目标 {target_price}")
        
        # === 决策 B: 止盈/正常模式 ===
        else:
            # 优先挂卖一价 (追求成交速度)
            target_price = best_ask
            
            # 硬性要求：如果卖一价 < 成本价，且没到止损时间，那只能挂 成本价+1 Tick 等待
            # 除非你愿意亏本刷量
            min_profit_price = self.avg_cost + self.tick_size
            if target_price < min_profit_price:
                target_price = min_profit_price

        # 检查当前挂单是否需要调整
        if self.active_sell_orders:
            current_order = self.active_sell_orders[0]
            current_price = float(current_order['price'])
            
            # 止损追跌逻辑：如果我在止损，且市场价跌得比我挂单还低，必须撤单追
            if is_stop_loss and self.chase_ask and best_ask < current_price:
                logger.info(f"📉 止损追价: 市场卖一 {best_ask} < 挂单 {current_price}，撤单")
                self.cancel_existing_orders()
                return

            # 正常挂单偏离调整 (超过 1 tick 就调)
            if abs(current_price - target_price) >= self.tick_size:
                 # 防止频繁撤单：只有当新目标价更有利(更高) 或者 必须要降价卖出时才动
                 if (is_stop_loss and target_price < current_price) or (not is_stop_loss and target_price > current_price):
                     self.cancel_existing_orders()
            
            return

        # 价格保护：卖单不能低于买一 (防止 Taker)
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
            "postOnly": True,   # 核心：只要是 Taker 就自动取消
            "timeInForce": "GTC"
        }
        
        # Backpack 特有字段
        if self.exchange == "backpack":
            order["autoLendRedeem"] = True
            
        res = self.client.execute_order(order)
        
        if isinstance(res, dict) and "error" in res:
            err_msg = str(res['error'])
            # 如果是 PostOnly 被拒，说明价格穿过盘口了，这是正常的，下一轮会重新计算
            if "post" in err_msg.lower() or "maker" in err_msg.lower():
                pass 
            else:
                logger.error(f"下单失败: {err_msg}")
        else:
            if side == "Bid":
                self.active_buy_orders.append(res)
            else:
                self.active_sell_orders.append(res)

    def _after_fill_processed(self, fill_info: Dict[str, Any]) -> None:
        """成交后回调：切换状态"""
        super()._after_fill_processed(fill_info)
        
        side = fill_info.get("side")
        quantity = float(fill_info.get("quantity", 0))
        price = float(fill_info.get("price", 0))
        
        # 忽略极小碎股成交
        if quantity < self.min_order_size * 0.1: return

        if side == "Bid":
            logger.info(f"✅ 买入成交 {quantity} @ {price} -> 切换至 [SELLING]")
            self.state = "SELLING"
            self.held_quantity = quantity
            self.avg_cost = price
            self.hold_start_time = time.time()
            # 立即撤销可能剩余的买单
            self.cancel_existing_orders()
            
        elif side == "Ask":
            profit = (price - self.avg_cost) * quantity
            logger.info(f"💰 卖出成交 {quantity} @ {price} (盈亏: {profit:.4f} U) -> 切换至 [BUYING]")
            self.state = "IDLE" # 先切回 IDLE 让主循环判断
            self.held_quantity = 0
            # 立即撤销可能剩余的卖单
            self.cancel_existing_orders()
