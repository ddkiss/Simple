"""
永续合约做市策略模块。

此模块在现货做市策略的基础上扩展，提供专为永续合约设计的
开仓、平仓与仓位风险管理功能。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

# 全局函数导入已移除，现在使用客户端方法
from logger import setup_logger
from strategies.market_maker import MarketMaker, format_balance
from utils.helpers import round_to_precision, round_to_tick_size

logger = setup_logger("perp_market_maker")


class PerpetualMarketMaker(MarketMaker):
    """专为永续合约设计的做市策略。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        symbol: str,
        target_position: float = 0.0,
        max_position: float = 1.0,
        position_threshold: float = 0.1,
        inventory_skew: float = 0.0,
        leverage: float = 1.0,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        ws_proxy: Optional[str] = None,
        exchange: str = 'backpack',
        exchange_config: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        """
        初始化永续合约做市策略。

        Args:
            api_key (str): API密钥。
            secret_key (str): API私钥。
            symbol (str): 交易对。
            target_position (float): 目标持仓量 (绝对值)，策略会将仓位大小维持在此数值附近。
            max_position (float): 最大允许持仓量（绝对值）。
            position_threshold (float): 触发仓位调整的阈值。
            inventory_skew (float): 库存偏移，影响报价的不对称性，以将净仓位推向0。
            leverage (float): 杠杆倍数。
            ws_proxy (Optional[str]): WebSocket代理地址。
        """
        kwargs.setdefault("enable_rebalance", False)
        super().__init__(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            ws_proxy=ws_proxy,
            exchange=exchange,
            exchange_config=exchange_config,
            **kwargs,
        )

        # 核心修改：target_position 现在是绝对持仓量目标
        self.target_position = abs(target_position)
        self.max_position = max(abs(max_position), self.min_order_size)
        self.position_threshold = max(position_threshold, self.min_order_size)
        self.inventory_skew = max(0.0, min(1.0, inventory_skew))
        self.leverage = max(1.0, leverage)
        self.stop_loss = abs(stop_loss) if stop_loss not in (None, 0) else None
        self.take_profit = abs(take_profit) if take_profit and take_profit > 0 else None
        self.last_protective_action: Optional[str] = None

        self.position_state: Dict[str, Any] = {
            "net": 0.0,
            "avg_entry": 0.0,
            "direction": "FLAT",
            "unrealized": 0.0,
        }

        # 添加总成交量统计（以报价资产计价）
        self.total_volume_quote = 0.0
        self.session_total_volume_quote = 0.0

        logger.info(
            "初始化永续合约做市: %s | 目标持仓量: %s | 最大持仓量: %s | 触发阈值: %s",
            symbol,
            format_balance(self.target_position),
            format_balance(self.max_position),
            format_balance(self.position_threshold),
        )

        if self.stop_loss is not None:
            logger.info(
                "设定未实现止损触发值: %s %s",
                format_balance(self.stop_loss),
                self.quote_asset,
            )
        if self.take_profit is not None:
            logger.info(
                "设定未实现止盈触发值: %s %s",
                format_balance(self.take_profit),
                self.quote_asset,
            )
        self._update_position_state()

    def on_ws_message(self, stream, data):
        """处理WebSocket消息回调 - 保持父类处理流程"""
        super().on_ws_message(stream, data)

    def _after_fill_processed(self, fill_info: Dict[str, Any]) -> None:
        """更新永续合约成交量统计"""
        price = fill_info.get('price')
        quantity = fill_info.get('quantity')

        if price is None or quantity is None:
            return

        try:
            trade_volume = float(price) * float(quantity)
        except (TypeError, ValueError):
            return

        self.total_volume_quote += trade_volume
        self.session_total_volume_quote += trade_volume

        logger.debug(
            "永续合约成交后更新总成交量: +%.2f %s (累计 %.2f)",
            trade_volume,
            self.quote_asset,
            self.total_volume_quote,
        )

    # ------------------------------------------------------------------
    # 基础信息与工具方法 (此处函数未变动)
    # ------------------------------------------------------------------
    def get_net_position(self) -> float:
        """取得目前的永续合约净仓位。"""
        try:
            # 直接查询特定交易对的仓位
            result = self.client.get_positions(self.symbol)

            if isinstance(result, dict) and "error" in result:
                error_msg = result["error"]
                # 404 错误表示没有仓位，这是正常情况
                if "404" in error_msg or "RESOURCE_NOT_FOUND" in error_msg:
                    logger.debug("未找到 %s 的仓位记录(404)，仓位为0", self.symbol)
                    return 0.0
                else:
                    logger.info(f"result: {result}")
                    logger.error("查询仓位失败: %s", error_msg)
                    return 0.0
                
            if not isinstance(result, list):
                logger.warning("仓位API返回格式异常: %s", type(result))
                return 0.0
                
            # 如果返回空列表，说明没有该交易对的仓位
            if not result:
                logger.debug("未找到 %s 的仓位记录，仓位为0", self.symbol)
                return 0.0
            
            # 取第一个仓位（因为已经按symbol过滤了）
            position = result[0]
            net_quantity = float(position.get("netQuantity", 0))
            
            logger.debug("从API获取 %s 永续仓位: %s", self.symbol, net_quantity)
            return net_quantity
            
        except Exception as e:
            logger.error("查询永续仓位时发生错误: %s", e)
            # 发生错误时，fallback到本地计算（虽然可能不准确）
            logger.warning("使用本地计算的仓位作为备用")
            return self.total_bought - self.total_sold

    def _get_actual_position_info(self) -> Dict[str, Any]:
        """获取完整的仓位信息"""
        try:
            # 尝试查询特定symbol的仓位
            result = self.client.get_positions(self.symbol)
            
            # 如果特定symbol查询失败，尝试查询所有仓位
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"特定symbol查询失败: {result['error']}")
                logger.info("尝试查询所有仓位...")
                result = self.client.get_positions()
                
                # 从所有仓位中筛选当前symbol
                if isinstance(result, list):
                    for pos in result:
                        if pos.get('symbol') == self.symbol:
                            logger.info(f"从所有仓位中找到 {self.symbol}")
                            return pos
                    logger.warning(f"在所有仓位中未找到 {self.symbol}")
                    return {}
            
            if not isinstance(result, list) or not result:
                logger.info("API返回空仓位列表")
                return {}
            
            position_data = result[0]
            

            
            return position_data
            
        except Exception as e:
            logger.error(f"获取仓位信息时发生错误: {e}")
            return {}

    def _calculate_average_short_entry(self) -> float:
        """计算目前空头仓位的平均开仓价格。"""
        if not self.sell_trades:
            return 0.0

        sell_queue: List[Tuple[float, float]] = self.sell_trades.copy()
        for _, buy_quantity in self.buy_trades:
            remaining_buy = buy_quantity
            while remaining_buy > 0 and sell_queue:
                sell_price, sell_quantity = sell_queue[0]
                matched = min(remaining_buy, sell_quantity)
                remaining_buy -= matched
                if matched >= sell_quantity:
                    sell_queue.pop(0)
                else:
                    sell_queue[0] = (sell_price, sell_quantity - matched)

        unmatched_quantity = sum(quantity for _, quantity in sell_queue)
        if unmatched_quantity <= 0:
            return 0.0

        unmatched_notional = sum(price * quantity for price, quantity in sell_queue)
        return unmatched_notional / unmatched_quantity

    def _update_position_state(self) -> None:
        """更新仓位相关统计。"""
        net = self.get_net_position()  # 现在这会从API获取实际仓位
        current_price = self.get_current_price()
        direction = "FLAT"
        avg_entry = 0.0
        unrealized = 0.0

        # 尝试从API获取更准确的信息
        position_info = self._get_actual_position_info()
        
        if position_info:
            # 使用API返回的精确信息
            entry_price_raw = position_info.get("entryPrice", 0)
            pnl_raw = position_info.get("pnlUnrealized", 0)

            # 安全处理可能为 None 的值
            avg_entry = float(entry_price_raw) if entry_price_raw is not None else 0.0
            unrealized = float(pnl_raw) if pnl_raw is not None else 0.0
        else:
            # 使用本地计算作为备用
            if net > 0:
                avg_entry = self._calculate_average_buy_cost()
                if current_price:
                    unrealized = (current_price - avg_entry) * net
            elif net < 0:
                avg_entry = self._calculate_average_short_entry()
                if current_price:
                    unrealized = (avg_entry - current_price) * abs(net)

        if net > 0:
            direction = "LONG"
        elif net < 0:
            direction = "SHORT"

        self.position_state = {
            "net": net,
            "avg_entry": avg_entry,
            "direction": direction,
            "unrealized": unrealized,
            "target": self.target_position,
            "max_position": self.max_position,
            "threshold": self.position_threshold,
            "inventory_skew": self.inventory_skew,
            "leverage": self.leverage,
            "timestamp": datetime.utcnow().isoformat(),
            "current_price": current_price or 0.0,
        }

    def get_position_state(self) -> Dict[str, Any]:
        """取得仓位信息快照。"""
        self._update_position_state()
        return self.position_state

    def _get_extra_summary_sections(self):
        """输出永续合约特有的成交量统计。"""
        sections = list(super()._get_extra_summary_sections())
        sections.append(
            (
                "永续合约成交量",
                [
                    ("累计总成交量", f"{self.total_volume_quote:.2f} {self.quote_asset}"),
                    ("本次执行成交量", f"{self.session_total_volume_quote:.2f} {self.quote_asset}"),
                ],
            )
        )
        return sections

    def run(self, duration_seconds=3600, interval_seconds=60):
        """执行永续合约做市策略"""
        logger.info(f"开始运行永续合约做市策略: {self.symbol}")

        # 重置本次执行的总成交量统计
        self.session_total_volume_quote = 0.0

        # 调用父类的 run 方法
        super().run(duration_seconds, interval_seconds)

    def check_stop_conditions(self, realized_pnl, unrealized_pnl, session_realized_pnl) -> bool:
        """强制更新仓位状态并检查止损止盈"""
        if self.stop_loss is None and self.take_profit is None:
            return False

        # 强制更新仓位状态
        self._update_position_state()
        position_state = self.get_position_state()
        
        net = float(position_state.get("net", 0.0))
        if math.isclose(net, 0.0, abs_tol=self.min_order_size / 10):
            logger.debug("没有活跃仓位，跳过止损止盈检查")
            return False

        # 获取未实现盈亏
        unrealized = float(position_state.get("unrealized", 0.0))
        
        # 如果API未实现盈亏为0但有仓位，手动计算
        if unrealized == 0.0 and net != 0.0:
            current_price = self.get_current_price()
            entry_price = float(position_state.get("avg_entry", 0.0))
            
            if current_price and entry_price:
                if net > 0:  # 多头
                    calculated_pnl = (current_price - entry_price) * net
                else:  # 空头
                    calculated_pnl = (entry_price - current_price) * abs(net)
                
                logger.warning(f"API未实现盈亏为0，手动计算: {calculated_pnl:.4f}")
                unrealized = calculated_pnl

        trigger_label = None
        trigger_threshold = None

        # 检查止损止盈条件
        if self.stop_loss is not None and unrealized <= -self.stop_loss:
            trigger_label = "止损"
            trigger_threshold = -self.stop_loss
        elif self.take_profit is not None and unrealized >= self.take_profit:
            trigger_label = "止盈"
            trigger_threshold = self.take_profit

        # 记录检查状态
        logger.info(f"止损止盈检查: 净仓位={net:.3f}, 未实现盈亏={unrealized:.4f}, "
                f"止损={-self.stop_loss if self.stop_loss else 'N/A'}, "
                f"止盈={self.take_profit if self.take_profit else 'N/A'}")

        if not trigger_label:
            return False

        logger.warning(f"*** {trigger_label}条件触发! ***")
        logger.warning(f"未实现盈亏: {unrealized:.4f} {self.quote_asset}")
        logger.warning(f"触发阈值: {trigger_threshold:.4f} {self.quote_asset}")

        # 执行紧急平仓
        try:
            logger.info("执行紧急平仓...")
            self.cancel_existing_orders()
            close_success = self.close_position(order_type="Market")

            if close_success:
                self.last_protective_action = (
                    f"{trigger_label}触发，已执行紧急平仓 "
                    f"(未实现盈亏 {unrealized:.4f} {self.quote_asset})"
                )
                logger.warning(f"紧急平仓成功: {self.last_protective_action}")
                return False  # 平仓后继续运行
            else:
                self.stop_reason = f"{trigger_label}触发但平仓失败，请立即检查仓位！"
                logger.error(self.stop_reason)
                return True  # 停止策略

        except Exception as e:
            logger.error(f"执行紧急平仓时出错: {e}")
            self.stop_reason = f"{trigger_label}触发，平仓过程中出现错误: {e}"
            return True

    # ------------------------------------------------------------------
    # 下单相关 (此处函数未变动)
    # ------------------------------------------------------------------
    def open_position(
        self,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "Limit",
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        client_id: Optional[str] = None,
    ) -> Dict:
        """提交开仓或平仓订单。"""

        normalized_order_type = order_type.capitalize()
        if normalized_order_type not in {"Limit", "Market"}:
            raise ValueError("order_type 仅支持 'Limit' 或 'Market'")

        qty = round_to_precision(abs(quantity), self.base_precision)
        if qty < self.min_order_size:
            logger.warning(
                "下单数量 %s 低于最小单位 %s，取消下单",
                format_balance(qty),
                format_balance(self.min_order_size),
            )
            return {"error": "quantity_too_small"}

        order_details: Dict[str, object] = {
            "orderType": normalized_order_type,
            "quantity": str(qty),
            "side": side,
            "symbol": self.symbol,
            "reduceOnly": reduce_only,
        }

        if normalized_order_type == "Limit":
            # Parameter 'timeInForce' sent when not required in Aster
            order_details["timeInForce"] = time_in_force
            if price is None:
                raise ValueError("Limit 订单需要提供价格")
            price_value = round_to_tick_size(price, self.tick_size)
            order_details["price"] = str(price_value)
        else:
            # 使用当前深度推估价格方便记录
            bid_price, ask_price = self.get_market_depth()
            reference_price = ask_price if side == "Bid" else bid_price
            if reference_price:
                logger.debug(
                    "使用市场价格 %.8f 作为 %s 市价订单参考",
                    reference_price,
                    side,
                )

        if client_id:
            order_details["clientId"] = str(client_id)

        logger.info(
            "提交永续合约订单: %s %s %s | reduceOnly=%s | 类型=%s",
            side,
            format_balance(qty),
            self.symbol,
            reduce_only,
            normalized_order_type,
        )

        result = self.client.execute_order(order_details)
        if isinstance(result, dict) and "error" in result:
            logger.error(f"永续合约订单失败: {result['error']}, 订单信息：{order_details}")
        else:
            self.orders_placed += 1
            logger.info("永续合约订单提交成功: %s", result.get("id", "unknown"))

        return result

    def open_long(
        self,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "Limit",
        reduce_only: bool = False,
        **kwargs,
    ) -> Dict:
        """开启或增加多头仓位。"""
        return self.open_position(
            side="Bid",
            quantity=quantity,
            price=price,
            order_type=order_type,
            reduce_only=reduce_only,
            **kwargs,
        )

    def open_short(
        self,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "Limit",
        reduce_only: bool = False,
        **kwargs,
    ) -> Dict:
        """开启或增加空头仓位。"""
        return self.open_position(
            side="Ask",
            quantity=quantity,
            price=price,
            order_type=order_type,
            reduce_only=reduce_only,
            **kwargs,
        )

    def close_position(
        self,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        order_type: str = "Market",
        side: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> bool:
        """平仓操作。"""
        net = self.get_net_position()  # 使用API获取实际仓位
        if math.isclose(net, 0.0, abs_tol=self.min_order_size / 10):
            logger.info("实际仓位为零，无需平仓")
            return True

        # 根据实际仓位确定平仓方向
        if net > 0:  # 多头仓位，需要卖出平仓
            direction = "long"
            order_side = "Ask"
        else:  # 空头仓位，需要买入平仓
            direction = "short"
            order_side = "Bid"

        # 如果手动指定了side，则使用指定的
        if side is not None:
            direction = side
            order_side = "Ask" if direction == "long" else "Bid"

        qty_available = abs(net)
        qty = qty_available if quantity is None else min(abs(quantity), qty_available)
        qty = round_to_precision(qty, self.base_precision)
        if qty < self.min_order_size:
            logger.info("平仓数量 %s 低于最小单位，忽略", format_balance(qty))
            return False

        logger.info(
            "执行平仓: 实际仓位=%s, 平仓数量=%s, 方向=%s",
            format_balance(net),
            format_balance(qty),
            direction
        )

        result = self.open_position(
            side=order_side,
            quantity=qty,
            price=price,
            order_type=order_type,
            reduce_only=True,
            client_id=client_id,
        )

        if isinstance(result, dict) and "error" in result:
            logger.error(
                "平仓失败: %s | side=%s qty=%s price=%s type=%s reduceOnly=%s clientId=%s result=%s",
                result.get("error"),
                order_side,
                format_balance(qty),
                price if price is not None else "Market",
                order_type,
                True,
                client_id or "",
                result,
            )
            return False

        logger.info("平仓完成，数量 %s", format_balance(qty))
        self._update_position_state()
        return True

    # ------------------------------------------------------------------
    # 仓位管理 (核心修改)
    # ------------------------------------------------------------------
    def need_rebalance(self) -> bool:
        """判断是否需要仓位调整 (仅减仓)。"""
        net = self.get_net_position()
        current_size = abs(net)

        if current_size > self.max_position:
            logger.warning(
                "持仓量 %s 超过最大允许 %s，准备执行风控平仓",
                format_balance(current_size),
                format_balance(self.max_position),
            )
            return True

        # 检查是否超过目标 + 阈值（而非仅仅偏离目标）
        threshold_line = self.target_position + self.position_threshold
        if current_size > threshold_line:
            logger.info(
                "持仓量 %s 超过阈值线 %s (目标 %s + 阈值 %s)，触发减仓调整",
                format_balance(current_size),
                format_balance(threshold_line),
                format_balance(self.target_position),
                format_balance(self.position_threshold)
            )
            return True

        return False

    def manage_positions(self) -> bool:
        """根据持仓量状态主动调整，此函数只负责减仓以控制风险。"""
        net = self.get_net_position()
        current_size = abs(net)
        desired_size = self.target_position

        logger.debug(
            "仓位管理: 实际持仓量=%s, 目标持仓量=%s",
            format_balance(current_size),
            format_balance(desired_size)
        )

        # 1. 风控：检查是否超过最大持仓量
        if current_size > self.max_position:
            excess = current_size - self.max_position
            logger.warning(
                "持仓量 %s 超过最大允许 %s，执行紧急平仓 %s",
                format_balance(current_size),
                format_balance(self.max_position),
                format_balance(excess)
            )
            return self.close_position(quantity=excess, order_type="Market")

        # 2. 减仓：检查持仓量是否超过目标 + 阈值
        # 只有当实际持仓超过 (目标持仓 + 阈值) 时才减仓
        threshold_line = desired_size + self.position_threshold
        if current_size > threshold_line:
            # 只平掉超出阈值线的部分，而不是全部
            qty_to_close = current_size - threshold_line
            logger.info(
                "持仓量 %s 超过阈值线 %s (目标 %s + 阈值 %s)，执行减仓 %s",
                format_balance(current_size),
                format_balance(threshold_line),
                format_balance(desired_size),
                format_balance(self.position_threshold),
                format_balance(qty_to_close)
            )
            # close_position 会根据净仓位自动判断平仓方向
            return self.close_position(quantity=qty_to_close, order_type="Market")

        logger.debug("持仓量在目标范围内，无需主动管理。")
        return False

    def rebalance_position(self) -> None:
        """覆写现货逻辑，改为永续仓位管理。"""
        logger.info("执行永续仓位管理")
        acted = self.manage_positions()
        if not acted:
            logger.info("仓位已在安全范围内，无需调整")

    # ------------------------------------------------------------------
    # 报价调整 (核心修改)
    # ------------------------------------------------------------------
    def calculate_prices(self):  # type: ignore[override]
        """计算买卖订单价格，并根据净仓位进行偏移以控制方向风险。"""
        buy_prices, sell_prices = super().calculate_prices()
        if not buy_prices or not sell_prices:
            return buy_prices, sell_prices

        net = self.get_net_position()
        current_price = self.get_current_price()
        
        # 获取盘口信息
        orderbook = self.client.get_order_book(self.symbol)
        best_bid = best_ask = None
        if orderbook and 'bids' in orderbook and 'asks' in orderbook:
            if orderbook['bids']:
                best_bid = float(orderbook['bids'][0][0])
            if orderbook['asks']:
                best_ask = float(orderbook['asks'][0][0])
        
        # 输出盘口和持仓信息
        logger.info("=== 市场状态 ===")
        if best_bid and best_ask:
            spread = best_ask - best_bid
            spread_pct = (spread / current_price * 100) if current_price else 0
            logger.info(f"盘口: Bid {best_bid:.4f} | Ask {best_ask:.4f} | 价差 {spread:.4f} ({spread_pct:.4f}%)")
        if current_price:
            logger.info(f"中间价: {current_price:.4f}")
        
        # 输出持仓信息
        direction = "空头" if net < 0 else "多头" if net > 0 else "无仓位"
        logger.info(f"持仓: {direction} {abs(net):.4f}  | 目标: {self.target_position:.4f} | 上限: {self.max_position:.1f}")

        # 如果没有库存偏移系数或没有仓位，则不进行调整
        if self.inventory_skew <= 0 or abs(net) < self.min_order_size:
            logger.info(f"原始挂单: 买 {buy_prices[0]:.4f} | 卖 {sell_prices[0]:.4f} (无偏移)")
            return buy_prices, sell_prices

        if self.max_position <= 0:
            return buy_prices, sell_prices

        # 核心偏移逻辑：根据净仓位(net)调整报价，目标是将净仓位推向0 (Delta中性)
        # 偏离量就是净仓位本身
        deviation = net
        skew_ratio = max(-1.0, min(1.0, deviation / self.max_position))

        if not current_price:
            return buy_prices, sell_prices

        # 如果是多头 (net > 0)，skew_offset为正；如果是空头 (net < 0)，skew_offset为负
        skew_offset = current_price * self.inventory_skew * skew_ratio

        # 调整价格以鼓励反向交易，使净仓位回归0
        # 如果是多头 (net > 0)，降低买卖价以鼓励市场吃掉我们的卖单，同时降低我们买入的意愿
        # 如果是空头 (net < 0)，提高买卖价以鼓励市场吃掉我们的买单，同时降低我们卖出的意愿
        adjusted_buys = [
            round_to_tick_size(price - skew_offset, self.tick_size)
            for price in buy_prices
        ]
        adjusted_sells = [
            round_to_tick_size(price - skew_offset, self.tick_size)
            for price in sell_prices
        ]

        # 输出价格调整详情
        logger.info("=== 价格计算 ===")
        logger.info(f"原始挂单: 买 {buy_prices[0]:.4f} | 卖 {sell_prices[0]:.4f}")
        logger.info(f"偏移计算: 净持仓 {net:.4f} | 偏移系数 {self.inventory_skew:.3f} | 偏移量 {skew_offset:.4f}")
        logger.info(f"调整后挂单: 买 {adjusted_buys[0]:.4f} | 卖 {adjusted_sells[0]:.4f}")

        # 风控：确保调整后买卖价没有交叉
        if adjusted_buys[0] >= adjusted_sells[0]:
            logger.warning("报价调整后买卖价交叉或价差过小，恢复原始报价。买: %s, 卖: %s", adjusted_buys[0], adjusted_sells[0])
            return buy_prices, sell_prices

        return adjusted_buys, adjusted_sells

    # ------------------------------------------------------------------
    # 其他辅助方法 (核心修改)
    # ------------------------------------------------------------------
    def set_target_position(self, target: float, threshold: Optional[float] = None) -> None:
        """更新目标持仓量 (绝对值) 及触发阈值。"""
        self.target_position = abs(target)
        if threshold is not None:
            self.position_threshold = max(threshold, self.min_order_size)
        logger.info(
            "更新目标持仓量: %s (阈值: %s)",
            format_balance(self.target_position),
            format_balance(self.position_threshold),
        )
        self._update_position_state()

    def set_max_position(self, max_position: float) -> None:
        """更新最大允许仓位。"""
        self.max_position = max(abs(max_position), self.min_order_size)
        logger.info(
            "更新最大仓位限制: %s",
            format_balance(self.max_position),
        )
        self._update_position_state()
