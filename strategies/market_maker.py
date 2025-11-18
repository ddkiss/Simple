"""
做市策略模块,全部成交才触发下一次事件
"""
import time
import threading
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Union, Any, Set, Deque
from concurrent.futures import ThreadPoolExecutor

from api.bp_client import BPClient
from api.aster_client import AsterClient
from api.lighter_client import LighterClient
from ws_client.client import BackpackWebSocket
from database.db import Database
from utils.helpers import round_to_precision, round_to_tick_size, calculate_volatility
from logger import setup_logger
import traceback

logger = setup_logger("market_maker")

def format_balance(value, decimals=8, threshold=1e-8) -> str:
    """
    格式化余额显示，避免科学记号
    
    Args:
        value: 数值
        decimals: 小数位数
        threshold: 阈值，小于此值显示为0
    """
    if abs(value) < threshold:
        return "0.00000000"
    return f"{value:.{decimals}f}"

class MarketMaker:
    def __init__(
        self,
        api_key,
        secret_key,
        symbol,
        db_instance=None,
        base_spread_percentage=0.2,
        order_quantity=None,
        max_orders=3,
        rebalance_threshold=15.0,
        enable_rebalance=True,
        base_asset_target_percentage=30.0,
        ws_proxy=None,
        exchange='backpack',
        exchange_config=None,
        wait_all_filled: bool = True,
        enable_database=False
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.symbol = symbol
        self.base_spread_percentage = base_spread_percentage
        self.order_quantity = order_quantity
        self.exchange = exchange
        self.exchange_config = exchange_config or {}
        
        # 初始化交易所客户端
        if exchange == 'backpack':
            self.client = BPClient(self.exchange_config)
        elif exchange == 'aster':
            self.client = AsterClient(self.exchange_config)
        elif exchange == 'paradex':
            from api.paradex_client import ParadexClient
            self.client = ParadexClient(self.exchange_config)
        elif exchange == 'lighter':
            self.client = LighterClient(self.exchange_config)
        else:
            raise ValueError(f"不支持的交易所: {exchange}")
            
        self.max_orders = max_orders
        self.rebalance_threshold = rebalance_threshold
        
        # 新增重平设置参数
        self.enable_rebalance = enable_rebalance
        self.base_asset_target_percentage = base_asset_target_percentage
        self.quote_asset_target_percentage = 100.0 - base_asset_target_percentage

        # 初始化数据库
        self.db_enabled = bool(enable_database)
        self.db = None
        if self.db_enabled:
            self.db = db_instance if db_instance else Database()
        elif db_instance and hasattr(db_instance, 'close'):
            try:
                db_instance.close()
            except Exception:
                pass

        if not self.db:
            self.db_enabled = False

        if not self.db_enabled:
            logger.info("数据库写入功能已关闭，本次执行仅在内存中追踪交易统计。")
        
        # 新增：控制挂单调整触发逻辑
        self.wait_all_filled = wait_all_filled
        self.last_trades_count = 0
        self.force_adjust_spread = self.base_spread_percentage * 5  # 强制调整 spread 阈值（可自订）
        self.last_adjust_price = None  # 上次调整时的价格，初始 None 表示首次需调整
        
        # 统计属性
        self.session_start_time = datetime.now()
        self.session_buy_trades = []
        self.session_sell_trades = []

        # 停止标志
        self._stop_flag = False
        self.session_fees = 0.0
        self.session_maker_buy_volume = 0.0
        self.session_maker_sell_volume = 0.0
        self.session_taker_buy_volume = 0.0
        self.session_taker_sell_volume = 0.0
        self.session_quote_volume = 0.0

        # 初始化市场限制
        self.market_limits = self.client.get_market_limits(symbol)
        if not self.market_limits:
            raise ValueError(f"无法获取 {symbol} 的市场限制")
        
        self.base_asset = self.market_limits['base_asset']
        self.quote_asset = self.market_limits['quote_asset']
        self.base_precision = self.market_limits['base_precision']
        self.quote_precision = self.market_limits['quote_precision']
        self.min_order_size = float(self.market_limits['min_order_size'])
        self.tick_size = float(self.market_limits['tick_size'])
        
        # 交易量统计
        self.maker_buy_volume = 0
        self.maker_sell_volume = 0
        self.taker_buy_volume = 0
        self.taker_sell_volume = 0
        self.total_quote_volume = 0.0
        self.total_fees = 0

        # 关键：在任何可能出错的代码之前初始化这些属性
        # 跟踪活跃订单
        self.active_buy_orders = []
        self.active_sell_orders = []
        
        # 记录买卖数量以便重新平衡
        self.total_bought = 0
        self.total_sold = 0
        
        # 交易记录 - 用于计算利润
        self.buy_trades = []
        self.sell_trades = []

        # 利润统计
        self.total_profit = 0
        self.trades_executed = 0
        self.orders_placed = 0
        self.orders_cancelled = 0


        # 风控状态
        self._stop_trading = False
        self.stop_reason: Optional[str] = None

        # WebSocket 重连冷却时间追踪
        self._last_reconnect_attempt = 0

        # 添加代理参数
        self.ws_proxy = ws_proxy
        # 建立WebSocket连接（仅对Backpack）
        if exchange == 'backpack':
            self.ws = BackpackWebSocket(api_key, secret_key, symbol, self.on_ws_message, auto_reconnect=True, proxy=self.ws_proxy)
            self.ws.connect()
        elif exchange == 'xx':
            ...
            self.ws = None
        else:
            self.ws = None  # 不使用WebSocket
        # 线程池用于后台任务
        self.executor = ThreadPoolExecutor(max_workers=3)

        # Aster REST 成交处理状态
        self._fill_history_bootstrapped = False
        self._processed_fill_ids: Set[str] = set()
        self._recent_fill_ids: Deque[str] = deque(maxlen=500)
        self._last_fill_timestamp: int = 0

        # 等待WebSocket连接建立并进行初始化订阅
        self._initialize_websocket()

        # 加载交易统计和历史交易
        self._load_trading_stats()
        self._load_recent_trades()

        # 针对无 WebSocket 的交易所使用 REST 成交同步
        if self.exchange in ('aster', 'lighter'):
            self._bootstrap_fill_history()
        
        logger.info(f"初始化做市商: {symbol}")
        logger.info(f"基础资产: {self.base_asset}, 报价资产: {self.quote_asset}")
        logger.info(f"基础精度: {self.base_precision}, 报价精度: {self.quote_precision}")
        logger.info(f"最小订单大小: {self.min_order_size}, 价格步长: {self.tick_size}")
        logger.info(f"基础价差百分比: {self.base_spread_percentage}%, 最大订单数: {self.max_orders}")
        logger.info(f"重平功能: {'开启' if self.enable_rebalance else '关闭'}")
        if self.enable_rebalance:
            logger.info(f"重平目标比例: {self.base_asset_target_percentage}% {self.base_asset} / {self.quote_asset_target_percentage}% {self.quote_asset}")
            logger.info(f"重平触发阈值: {self.rebalance_threshold}%")

    def _db_available(self) -> bool:
        """检查数据库功能是否启用且可用。"""
        return self.db_enabled and self.db is not None

    def set_rebalance_settings(self, enable_rebalance=None, base_asset_target_percentage=None, rebalance_threshold=None):
        """
        设置重平参数
        
        Args:
            enable_rebalance: 是否开启重平功能
            base_asset_target_percentage: 基础资产目标比例 (0-100)
            rebalance_threshold: 重平触发阈值
        """
        if enable_rebalance is not None:
            self.enable_rebalance = enable_rebalance
            logger.info(f"重平功能设置为: {'开启' if enable_rebalance else '关闭'}")
        
        if base_asset_target_percentage is not None:
            if not 0 <= base_asset_target_percentage <= 100:
                raise ValueError("基础资产目标比例必须在0-100之间")
            
            self.base_asset_target_percentage = base_asset_target_percentage
            self.quote_asset_target_percentage = 100.0 - base_asset_target_percentage
            logger.info(f"重平目标比例设置为: {self.base_asset_target_percentage}% {self.base_asset} / {self.quote_asset_target_percentage}% {self.quote_asset}")
        
        if rebalance_threshold is not None:
            if rebalance_threshold <= 0:
                raise ValueError("重平触发阈值必须大于0")
            
            self.rebalance_threshold = rebalance_threshold
            logger.info(f"重平触发阈值设置为: {self.rebalance_threshold}%")
    
    def get_rebalance_settings(self):
        """
        获取当前重平设置
        
        Returns:
            dict: 重平设置信息
        """
        return {
            'enable_rebalance': self.enable_rebalance,
            'base_asset_target_percentage': self.base_asset_target_percentage,
            'quote_asset_target_percentage': self.quote_asset_target_percentage,
            'rebalance_threshold': self.rebalance_threshold
        }
    
    def get_total_balance(self):
        """获取总余额，包含普通余额和抵押品余额"""
        try:
            # 获取普通余额
            balances = self.client.get_balance()
            if isinstance(balances, dict) and "error" in balances:
                logger.error(f"获取普通余额失败: {balances['error']}")
                return None
            
            # 获取抵押品余额
            collateral = self.client.get_collateral()
            if isinstance(collateral, dict) and "error" in collateral:
                logger.warning(f"获取抵押品余额失败: {collateral['error']}")
                collateral_assets = []
            else:
                collateral_assets = collateral.get('assets') or collateral.get('collateral', [])
            
            # 初始化总余额字典
            total_balances = {}
            
            # 处理普通余额
            if isinstance(balances, dict):
                for asset, details in balances.items():
                    available = float(details.get('available', 0))
                    locked = float(details.get('locked', 0))
                    total_balances[asset] = {
                        'available': available,
                        'locked': locked,
                        'total': available + locked,
                        'collateral_available': 0,
                        'collateral_total': 0
                    }
            
            # 添加抵押品余额
            for item in collateral_assets:
                symbol = item.get('symbol', '')
                if symbol:
                    total_quantity = float(item.get('totalQuantity', 0))
                    available_quantity = float(item.get('availableQuantity', 0))
                    
                    if symbol not in total_balances:
                        total_balances[symbol] = {
                            'available': 0,
                            'locked': 0,
                            'total': 0,
                            'collateral_available': available_quantity,
                            'collateral_total': total_quantity
                        }
                    else:
                        total_balances[symbol]['collateral_available'] = available_quantity
                        total_balances[symbol]['collateral_total'] = total_quantity
                    
                    # 更新总可用量和总量
                    total_balances[symbol]['total_available'] = (
                        total_balances[symbol]['available'] + 
                        total_balances[symbol]['collateral_available']
                    )
                    total_balances[symbol]['total_all'] = (
                        total_balances[symbol]['total'] + 
                        total_balances[symbol]['collateral_total']
                    )
            
            # 确保所有资产都有total_available和total_all字段
            for asset in total_balances:
                if 'total_available' not in total_balances[asset]:
                    total_balances[asset]['total_available'] = total_balances[asset]['available']
                if 'total_all' not in total_balances[asset]:
                    total_balances[asset]['total_all'] = total_balances[asset]['total']
            
            return total_balances
            
        except Exception as e:
            logger.error(f"获取总余额时出错: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def get_asset_balance(self, asset):
        """获取指定资产的总可用余额"""
        total_balances = self.get_total_balance()
        if not total_balances or asset not in total_balances:
            return 0, 0  # 返回 (可用余额, 总余额)
        
        balance_info = total_balances[asset]
        available = balance_info.get('total_available', 0)
        total = balance_info.get('total_all', 0)
        
        # 格式化显示余额，避免科学记号
        normal_available = balance_info.get('available', 0)
        collateral_available = balance_info.get('collateral_available', 0)
        
        logger.debug(f"{asset} 余额详情: 普通可用={format_balance(normal_available)}, "
                    f"抵押品可用={format_balance(collateral_available)}, "
                    f"总可用={format_balance(available)}, 总量={format_balance(total)}")
        
        return available, total
    
    def _initialize_websocket(self):
        """等待WebSocket连接建立并进行初始化订阅"""
        if self.ws is None:
            logger.info("使用 REST API 模式（无 WebSocket）")
            return

        wait_time = 2
        max_wait_time = 10  # 减少等待时间从 10 秒到 2 秒
        check_interval = 0.2  # 减少检查间隔从 0.5 秒到 0.2 秒

        while not self.ws.connected and wait_time < max_wait_time:
            time.sleep(check_interval)
            wait_time += check_interval

        if self.ws.connected:
            logger.info("WebSocket连接已建立，初始化数据流...")

            # 初始化订单簿
            orderbook_initialized = self.ws.initialize_orderbook()

            # 订阅深度流和行情数据
            if orderbook_initialized:
                depth_subscribed = self.ws.subscribe_depth()
                ticker_subscribed = self.ws.subscribe_bookTicker()

                if depth_subscribed and ticker_subscribed:
                    logger.info("数据流订阅成功!")

            # 订阅私有订单更新流
            self.subscribe_order_updates()
        else:
            logger.info("WebSocket 初始连接未建立，使用 REST API 模式（WebSocket 将在后台自动重连）")
    
    def _load_trading_stats(self):
        """从数据库加载交易统计数据"""
        if not self._db_available():
            logger.debug("数据库未启用，跳过交易统计加载。")
            return
        try:
            today = datetime.now().strftime('%Y-%m-%d')

            # 查询今天的统计数据
            stats = self.db.get_trading_stats(self.symbol, today)
            
            if stats and len(stats) > 0:
                stat = stats[0]
                self.maker_buy_volume = stat['maker_buy_volume']
                self.maker_sell_volume = stat['maker_sell_volume']
                self.taker_buy_volume = stat['taker_buy_volume']
                self.taker_sell_volume = stat['taker_sell_volume']
                self.total_profit = stat['realized_profit']
                self.total_fees = stat['total_fees']
                
                logger.info(f"已从数据库加载今日交易统计")
                logger.info(f"Maker买入量: {self.maker_buy_volume}, Maker卖出量: {self.maker_sell_volume}")
                logger.info(f"Taker买入量: {self.taker_buy_volume}, Taker卖出量: {self.taker_sell_volume}")
                logger.info(f"已实现利润: {self.total_profit}, 总手续费: {self.total_fees}")
            else:
                logger.info("今日无交易统计记录，将创建新记录")
        except Exception as e:
            logger.error(f"加载交易统计时出错: {e}")
    
    def _load_recent_trades(self):
        """从数据库加载历史成交记录"""
        if not self._db_available():
            logger.debug("数据库未启用，跳过历史成交加载。")
            return
        try:
            # 获取订单历史
            trades = self.db.get_order_history(self.symbol, 1000)
            trades_count = len(trades) if trades else 0
            
            if trades_count > 0:
                for side, quantity, price, maker, fee in trades:
                    quantity = float(quantity)
                    price = float(price)
                    fee = float(fee)
                    quote_volume = abs(quantity * price)

                    if side == 'Bid':  # 买入
                        self.buy_trades.append((price, quantity))
                        self.total_bought += quantity
                        self.total_quote_volume += quote_volume
                        if maker:
                            self.maker_buy_volume += quantity
                        else:
                            self.taker_buy_volume += quantity
                    elif side == 'Ask':  # 卖出
                        self.sell_trades.append((price, quantity))
                        self.total_sold += quantity
                        self.total_quote_volume += quote_volume
                        if maker:
                            self.maker_sell_volume += quantity
                        else:
                            self.taker_sell_volume += quantity
                    
                    self.total_fees += fee
                
                logger.info(f"已从数据库加载 {trades_count} 条历史成交记录")
                logger.info(f"总买入: {self.total_bought} {self.base_asset}, 总卖出: {self.total_sold} {self.base_asset}")
                logger.info(f"Maker买入: {self.maker_buy_volume} {self.base_asset}, Maker卖出: {self.maker_sell_volume} {self.base_asset}")
                logger.info(f"Taker买入: {self.taker_buy_volume} {self.base_asset}, Taker卖出: {self.taker_sell_volume} {self.base_asset}")
                
                # 计算精确利润
                self.total_profit = self._calculate_db_profit()
                logger.info(f"计算得出已实现利润: {self.total_profit:.8f} {self.quote_asset}")
                logger.info(f"总手续费: {self.total_fees:.8f} {self.quote_asset}")
            else:
                logger.info("数据库中没有历史成交记录，将开始记录新的交易")
                
        except Exception as e:
            logger.error(f"加载历史成交记录时出错: {e}")
            import traceback
            traceback.print_exc()

    # ------------------------------------------------------------------
    # Aster REST 成交同步相关方法
    # ------------------------------------------------------------------
    def _bootstrap_fill_history(self) -> None:
        """初始化 REST 成交历史，避免重复计数"""
        exchange_label = self.exchange.capitalize()
        try:
            self._sync_fill_history(bootstrap=True)
            logger.info("%s 成交历史初始化完成，开始追踪新成交", exchange_label)
        except Exception as e:
            logger.error(f"初始化 {exchange_label} 成交历史时出错: {e}")

    def _sync_fill_history(self, bootstrap: bool = False) -> None:
        """透过 REST API 同步最新成交"""
        if self.exchange not in ('aster', 'lighter'):
            return

        exchange_label = self.exchange.capitalize()

        try:
            response = self.client.get_fill_history(self.symbol, limit=200)
        except Exception as e:
            logger.error(f"获取 {exchange_label} 成交历史时出错: {e}")
            return

        fills = self._normalize_fill_history_response(response)
        if not fills:
            return

        fills.sort(key=lambda item: item.get('timestamp', 0))

        if bootstrap or not self._fill_history_bootstrapped:
            for fill in fills:
                self._register_processed_fill(fill.get('fill_id'), fill.get('timestamp', 0))
            self._fill_history_bootstrapped = True
            return

        for fill in fills:
            fill_id = fill.get('fill_id')
            timestamp = fill.get('timestamp', 0)

            if self._has_seen_fill(fill_id, timestamp):
                continue

            order_id = fill.get('order_id')
            side = fill.get('side')
            quantity = fill.get('quantity')
            price = fill.get('price')

            if not order_id or quantity is None or price is None:
                continue

            maker = fill.get('is_maker', True)
            fee = fill.get('fee', 0.0)
            fee_asset = fill.get('fee_asset') or self.quote_asset

            normalized_side = None
            if isinstance(side, str):
                side_upper = side.upper()
                if side_upper in ('BUY', 'BID'):
                    normalized_side = 'Bid'
                elif side_upper in ('SELL', 'ASK'):
                    normalized_side = 'Ask'

            if normalized_side is None:
                continue

            self._register_processed_fill(fill_id, timestamp)
            self._process_order_fill_event(
                side=normalized_side,
                quantity=quantity,
                price=price,
                order_id=order_id,
                maker=maker,
                fee=fee,
                fee_asset=fee_asset,
                trade_id=fill_id,
                source='rest',
                timestamp=timestamp,
                register_processed=False,
            )

    def _normalize_fill_history_response(self, response) -> List[Dict[str, Any]]:
        """将 REST API 返回的成交数据转换为统一格式"""
        if isinstance(response, dict) and 'error' in response:
            logger.error(f"获取成交历史失败: {response['error']}")
            return []

        data = response
        if isinstance(response, dict):
            data = response.get('data', response)

        if not isinstance(data, list):
            logger.warning(f"成交历史返回格式异常: {type(data)}")
            return []

        fills: List[Dict[str, Any]] = []

        def _extract(entry: Dict[str, Any], *keys: str) -> Any:
            for key in keys:
                if key in entry and entry[key] not in (None, ""):
                    return entry[key]
            return None

        def _to_float(value: Any) -> Optional[float]:
            if value in (None, "", "NaN"):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        for entry in data:
            if not isinstance(entry, dict):
                continue

            fill_id = _extract(
                entry,
                "id",
                "fillId",
                "fill_id",
                "tradeId",
                "trade_id",
                "executionId",
                "execution_id",
                "t",
            )
            order_id = _extract(
                entry,
                "orderId",
                "order_id",
                "orderIndex",
                "order_index",
                "ask_id",
                "bid_id",
                "i",
            )
            side = _extract(entry, "side", "S")
            price = _to_float(_extract(entry, "price", "p", "L"))
            quantity = _to_float(_extract(entry, "quantity", "qty", "q", "l", "size"))
            fee_asset = _extract(
                entry,
                "fee_asset",
                "feeAsset",
                "commissionAsset",
                "N",
                "fee_currency",
                "feeCurrency",
            )
            maker_flag = _extract(entry, "maker", "isMaker", "m", "is_maker")
            timestamp_raw = _extract(entry, "time", "timestamp", "T", "ts")

            maker_fee = _to_float(_extract(entry, "maker_fee", "makerFee"))
            taker_fee = _to_float(_extract(entry, "taker_fee", "takerFee"))
            fee_primary = _extract(entry, "fee", "commission", "n", "fee_value")
            fee_value = _to_float(fee_primary)

            derived_maker_flag: Optional[bool] = None
            if maker_fee is not None and abs(maker_fee) > 0:
                derived_maker_flag = True
            elif taker_fee is not None and abs(taker_fee) > 0:
                derived_maker_flag = False

            is_maker = True
            if isinstance(maker_flag, bool):
                is_maker = maker_flag
            elif maker_flag is not None:
                is_maker = str(maker_flag).lower() in ("true", "1", "yes")
            elif derived_maker_flag is not None:
                is_maker = derived_maker_flag

            if fee_value is None:
                if is_maker and maker_fee is not None:
                    fee_value = maker_fee
                elif not is_maker and taker_fee is not None:
                    fee_value = taker_fee
                elif maker_fee is not None:
                    fee_value = maker_fee
                elif taker_fee is not None:
                    fee_value = taker_fee

            if fee_value is None:
                fee_value = 0.0

            try:
                timestamp_value = int(float(timestamp_raw)) if timestamp_raw is not None else 0
            except (TypeError, ValueError):
                timestamp_value = 0

            fills.append({
                'fill_id': str(fill_id) if fill_id is not None else None,
                'order_id': str(order_id) if order_id is not None else None,
                'side': side,
                'price': price,
                'quantity': quantity,
                'fee': fee_value,
                'fee_asset': fee_asset,
                'is_maker': is_maker,
                'timestamp': timestamp_value,
            })

        return fills

    def _has_seen_fill(self, fill_id: Optional[str], timestamp: int) -> bool:
        """判断成交是否已处理"""
        if fill_id and fill_id in self._processed_fill_ids:
            return True
        if (not fill_id or fill_id is None) and timestamp and timestamp <= self._last_fill_timestamp:
            return True
        return False

    def _register_processed_fill(self, fill_id: Optional[str], timestamp: int) -> None:
        """将成交标记为已处理"""
        if fill_id:
            if len(self._recent_fill_ids) >= self._recent_fill_ids.maxlen:
                oldest = self._recent_fill_ids.popleft()
                if oldest in self._processed_fill_ids:
                    self._processed_fill_ids.remove(oldest)
            self._recent_fill_ids.append(fill_id)
            self._processed_fill_ids.add(fill_id)

        if timestamp:
            self._last_fill_timestamp = max(self._last_fill_timestamp, timestamp)

    def _process_order_fill_event(
        self,
        *,
        side: str,
        quantity: float,
        price: float,
        order_id: Optional[str],
        maker: bool,
        fee: float,
        fee_asset: Optional[str],
        trade_id: Optional[str] = None,
        source: str = 'ws',
        timestamp: Optional[int] = None,
        register_processed: bool = True,
    ) -> None:
        """统一处理成交事件来源 (WebSocket/REST)"""

        if register_processed:
            self._register_processed_fill(trade_id, timestamp or 0)

        fee_asset = fee_asset or self.quote_asset

        normalized_side = side
        if isinstance(side, str):
            side_upper = side.upper()
            if side_upper in ("BUY", "BID"):
                normalized_side = "Bid"
            elif side_upper in ("SELL", "ASK"):
                normalized_side = "Ask"

        logger.info(
            f"订单成交[{source}]: ID={order_id}, 方向={normalized_side}, 数量={quantity}, 价格={price}, Maker={maker}, 手续费={fee:.8f}"
        )

        trade_type = 'market_making'
        if order_id and self._db_available():
            try:
                if self.db.is_rebalance_order(order_id, self.symbol):
                    trade_type = 'rebalance'
            except Exception as db_err:
                logger.error(f"检查重平衡订单时出错: {db_err}")

        order_data = {
            'order_id': order_id,
            'symbol': self.symbol,
            'side': normalized_side,
            'quantity': quantity,
            'price': price,
            'maker': maker,
            'fee': fee,
            'fee_asset': fee_asset,
            'trade_type': trade_type,
        }

        if self._db_available():
            def safe_insert_order():
                try:
                    self.db.insert_order(order_data)
                except Exception as db_err:
                    logger.error(f"插入订单数据时出错: {db_err}")

            safe_insert_order()

        trade_quote_volume = abs(quantity * price)
        self.total_quote_volume += trade_quote_volume
        self.session_quote_volume += trade_quote_volume

        if normalized_side == 'Bid':
            self.total_bought += quantity
            self.buy_trades.append((price, quantity))

            if maker:
                self.maker_buy_volume += quantity
                self.session_maker_buy_volume += quantity
            else:
                self.taker_buy_volume += quantity
                self.session_taker_buy_volume += quantity

            self.session_buy_trades.append((price, quantity))

        elif normalized_side == 'Ask':
            self.total_sold += quantity
            self.sell_trades.append((price, quantity))

            if maker:
                self.maker_sell_volume += quantity
                self.session_maker_sell_volume += quantity
            else:
                self.taker_sell_volume += quantity
                self.session_taker_sell_volume += quantity

            self.session_sell_trades.append((price, quantity))

        self.total_fees += fee
        self.session_fees += fee

        if self._db_available():
            def safe_update_stats_wrapper():
                try:
                    self._update_trading_stats()
                except Exception as e:
                    logger.error(f"更新交易统计时出错: {e}")

            self.executor.submit(safe_update_stats_wrapper)

        if self._db_available():
            def update_profit():
                try:
                    profit = self._calculate_db_profit()
                    self.total_profit = profit
                except Exception as e:
                    logger.error(f"更新利润计算时出错: {e}")

            self.executor.submit(update_profit)

        session_profit = self._calculate_session_profit()

        logger.info(f"累计利润: {self.total_profit:.8f} {self.quote_asset}")
        logger.info(f"本次执行利润: {session_profit:.8f} {self.quote_asset}")
        logger.info(f"本次执行手续费: {self.session_fees:.8f} {self.quote_asset}")
        logger.info(f"本次执行净利润: {(session_profit - self.session_fees):.8f} {self.quote_asset}")

        self.trades_executed += 1
        logger.info(f"总买入: {self.total_bought} {self.base_asset}, 总卖出: {self.total_sold} {self.base_asset}")
        logger.info(f"Maker买入: {self.maker_buy_volume} {self.base_asset}, Maker卖出: {self.maker_sell_volume} {self.base_asset}")
        logger.info(f"Taker买入: {self.taker_buy_volume} {self.base_asset}, Taker卖出: {self.taker_sell_volume} {self.base_asset}")

        fill_info = {
            'side': normalized_side,
            'quantity': quantity,
            'price': price,
            'order_id': order_id,
            'maker': maker,
            'fee': fee,
            'fee_asset': fee_asset,
            'trade_id': trade_id,
            'source': source,
            'timestamp': timestamp,
        }

        try:
            self._after_fill_processed(fill_info)
        except Exception as hook_error:
            logger.error(f"成交后置处理时出错: {hook_error}")

    def _after_fill_processed(self, fill_info: Dict[str, Any]) -> None:
        """留给子类覆盖的成交后置处理钩子"""
        return

    def check_ws_connection(self):
        """检查并恢复WebSocket连接"""
        if not self.ws:
            # aster 和 paradex 没有 WebSocket，直接返回 True
            if self.exchange in ('aster', 'paradex'):
                return True
            if self.exchange == 'lighter':
                return True
            logger.warning("WebSocket对象不存在，尝试重新创建...")
            return self._recreate_websocket()

        ws_connected = self.ws.is_connected()

        if not ws_connected and not getattr(self.ws, 'reconnecting', False):
            # 检查上次重连尝试的时间，避免频繁重连
            current_time = time.time()
            last_reconnect_attempt = getattr(self, '_last_reconnect_attempt', 0)
            reconnect_cooldown = 30  # 30秒冷却时间

            if current_time - last_reconnect_attempt >= reconnect_cooldown:
                logger.warning("WebSocket连接已断开，触发重连...")
                self._last_reconnect_attempt = current_time
                # 使用 WebSocket 自己的重连机制
                self.ws.check_and_reconnect_if_needed()
            else:
                remaining = int(reconnect_cooldown - (current_time - last_reconnect_attempt))
                logger.debug(f"WebSocket 重连冷却中，剩余 {remaining} 秒")

        return self.ws.is_connected() if self.ws else False
    
    def _recreate_websocket(self):
        """重新创建WebSocket连接"""
        try:
            if self.exchange == 'aster':
                logger.info(f"{self.exchange} 交易所不使用 WebSocket")
                return True
            
            # 安全关闭现有连接
            if self.ws:
                try:
                    self.ws.running = False
                    self.ws.close()
                    time.sleep(0.5)
                except Exception as e:
                    logger.debug(f"关闭现有WebSocket时的预期错误: {e}")
            if self.exchange == 'backpack':
                # 创建新的连接
                self.ws = BackpackWebSocket(
                    self.api_key, 
                    self.secret_key, 
                    self.symbol, 
                    self.on_ws_message, 
                    auto_reconnect=True,
                    proxy=self.ws_proxy
                )
            elif self.exchange == 'xx':
                ...
            self.ws.connect()
            
            # 等待连接建立，但不要等太久
            wait_time = 0
            max_wait_time = 3  # 减少等待时间
            while not self.ws.is_connected() and wait_time < max_wait_time:
                time.sleep(0.5)
                wait_time += 0.5
                
            if self.ws.is_connected():
                logger.info("WebSocket重新创建成功")
                
                # 重新初始化
                self.ws.initialize_orderbook()
                self.ws.subscribe_depth()
                self.ws.subscribe_bookTicker()
                self.subscribe_order_updates()
                return True
            else:
                logger.warning("WebSocket重新创建后仍未连接，但继续运行")
                return False
                
        except Exception as e:
            logger.error(f"重新创建WebSocket连接时出错: {e}")
            return False
    
    def on_ws_message(self, stream, data):
        """处理WebSocket消息回调"""
        if stream.startswith("account.orderUpdate."):
            event_type = data.get('e')
            
            # 「订单成交」事件
            if event_type == 'orderFill':
                try:
                    side = data.get('S')
                    quantity = float(data.get('l', '0'))
                    price = float(data.get('L', '0'))
                    order_id = data.get('i')
                    maker = data.get('m', False)
                    
                    # 解析手续费信息（处理各种可能的字段名）
                    fee = 0.0
                    fee_asset = self.quote_asset
                    fee_fields = [
                        ('n', 'N'),  # n: fee amount, N: fee asset
                        ('fee', 'fee_currency'),
                        ('commission', 'commissionAsset')
                    ]
                    for amount_field, asset_field in fee_fields:
                        fee_amount = data.get(amount_field)
                        if fee_amount is not None:
                            try:
                                fee = float(fee_amount)
                                fee_asset = data.get(asset_field, self.quote_asset)
                                break
                            except (TypeError, ValueError):
                                continue
                    
                    trade_id = data.get('t')
                    timestamp = data.get('T') or data.get('E')

                    trade_id_str = str(trade_id) if trade_id is not None else None
                    timestamp_int = None
                    if timestamp is not None:
                        try:
                            timestamp_int = int(timestamp)
                        except (TypeError, ValueError):
                            timestamp_int = None

                    logger.info(
                        f"WebSocket 成交通知: {'买' if side == 'BUY' else '卖'}单成交 "
                        f"{quantity} @ {price}, "
                        f"{'Maker' if maker else 'Taker'}, "
                        f"手续费: {fee} {fee_asset}"
                    )

                    self._process_order_fill_event(
                        side=side,
                        quantity=quantity,
                        price=price,
                        order_id=order_id,
                        maker=bool(maker),
                        fee=fee,
                        fee_asset=fee_asset,
                        trade_id=trade_id_str,
                        source=data.get('source', 'ws'),
                        timestamp=timestamp_int,
                    )
                    
                except Exception as e:
                    logger.error(f"处理订单成交消息时出错: {e}")
                    traceback.print_exc()
    
    def on_order_update(self, order_data):
        """处理所有交易所的订单更新消息 - 统一接口"""
        try:
            order_id = order_data.get('order_id')
            side = order_data.get('side', '').lower()
            status = order_data.get('status')
            filled_size = float(order_data.get('filled_size', '0'))
            price = float(order_data.get('price', '0'))
            
            # 简化日志输出 - 只记录重要的状态变化
            if status in ('FILLED', 'PARTIALLY_FILLED', 'filled', 'partial_filled'):
                if filled_size > 0:
                    direction = "买入" if side == 'buy' else "卖出"
                    logger.info(f"*** 成交通知: {direction} {filled_size:.3f} SOL @ {price:.3f} USDT ({status}) ***")
                
            # 通用处理逻辑 - 处理成交的订单
            if status in ('FILLED', 'PARTIALLY_FILLED', 'filled', 'partial_filled') and filled_size > 0:
                # 模拟订单成交数据格式
                is_maker = True  # 限价单通常是 maker
                
                # 准备订单数据用于数据库记录
                order_data_db = {
                    'order_id': order_id,
                    'symbol': self.symbol,
                    'side': 'Bid' if side == 'buy' else 'Ask',  # 转换为数据库格式
                    'quantity': filled_size,
                    'price': price,
                    'maker': is_maker,
                    'fee': 0.0,  # 手续费可能需要单独查询
                    'fee_asset': self.quote_asset,
                    'trade_type': 'market_making'
                }
                
                # 更新统计
                quote_volume = abs(filled_size * price)
                self.total_quote_volume += quote_volume
                self.session_quote_volume += quote_volume

                if side == 'buy':
                    self.total_bought += filled_size
                    if is_maker:
                        self.maker_buy_volume += filled_size
                        self.session_maker_buy_volume += filled_size
                    else:
                        self.taker_buy_volume += filled_size
                        self.session_taker_buy_volume += filled_size
                    self.buy_trades.append((price, filled_size))
                    self.session_buy_trades.append((price, filled_size))
                elif side == 'sell':
                    self.total_sold += filled_size
                    if is_maker:
                        self.maker_sell_volume += filled_size
                        self.session_maker_sell_volume += filled_size
                    else:
                        self.taker_sell_volume += filled_size
                        self.session_taker_sell_volume += filled_size
                    self.sell_trades.append((price, filled_size))
                    self.session_sell_trades.append((price, filled_size))
                
                # 异步插入数据库
                if self._db_available():
                    def safe_insert_order():
                        try:
                            self.db.insert_order(order_data_db)
                        except Exception as db_err:
                            logger.error(f"插入订单数据时出错: {db_err}")

                    self.executor.submit(safe_insert_order)

                    # 更新利润计算
                    def update_profit():
                        try:
                            profit = self._calculate_db_profit()
                            self.total_profit = profit
                        except Exception as e:
                            logger.error(f"更新利润计算时出错: {e}")

                    self.executor.submit(update_profit)
                
                # 执行统计报告
                session_profit = self._calculate_session_profit()
                
                logger.info(f"累计利润: {self.total_profit:.8f} {self.quote_asset}")
                logger.info(f"本次执行利润: {session_profit:.8f} {self.quote_asset}")
                logger.info(f"总买入: {self.total_bought} {self.base_asset}, 总卖出: {self.total_sold} {self.base_asset}")
                
                self.trades_executed += 1
                
        except Exception as e:
            logger.error(f"处理订单更新时出错: {e}")
            traceback.print_exc()
    
    def _calculate_memory_profit(self) -> float:
        """使用内存中的成交记录计算已实现利润（FIFO）。"""
        if not self.buy_trades or not self.sell_trades:
            return 0.0

        buy_queue: List[Tuple[float, float]] = [
            (float(price), float(quantity)) for price, quantity in self.buy_trades
        ]
        total_profit = 0.0

        for sell_price, sell_quantity in self.sell_trades:
            remaining_sell = float(sell_quantity)
            sell_price = float(sell_price)

            while remaining_sell > 0 and buy_queue:
                buy_price, buy_quantity = buy_queue[0]
                matched_quantity = min(remaining_sell, buy_quantity)

                total_profit += (sell_price - buy_price) * matched_quantity

                remaining_sell -= matched_quantity
                if matched_quantity >= buy_quantity:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (buy_price, buy_quantity - matched_quantity)

        return total_profit

    def _calculate_db_profit(self):
        """基于数据库记录计算已实现利润（FIFO方法）"""
        if not self._db_available():
            return self._calculate_memory_profit()
        try:
            # 获取订单历史，注意这里将返回一个列表
            order_history = self.db.get_order_history(self.symbol)
            if not order_history:
                return 0
            
            buy_trades = []
            sell_trades = []
            for side, quantity, price, maker, fee in order_history:
                if side == 'Bid':
                    buy_trades.append((float(price), float(quantity), float(fee)))
                elif side == 'Ask':
                    sell_trades.append((float(price), float(quantity), float(fee)))

            if not buy_trades or not sell_trades:
                return 0

            buy_queue = buy_trades.copy()
            total_profit = 0
            total_fees = 0

            for sell_price, sell_quantity, sell_fee in sell_trades:
                remaining_sell = sell_quantity
                total_fees += sell_fee

                while remaining_sell > 0 and buy_queue:
                    buy_price, buy_quantity, buy_fee = buy_queue[0]
                    matched_quantity = min(remaining_sell, buy_quantity)

                    trade_profit = (sell_price - buy_price) * matched_quantity
                    allocated_buy_fee = buy_fee * (matched_quantity / buy_quantity)
                    total_fees += allocated_buy_fee

                    net_trade_profit = trade_profit
                    total_profit += net_trade_profit

                    remaining_sell -= matched_quantity
                    if matched_quantity >= buy_quantity:
                        buy_queue.pop(0)
                    else:
                        remaining_fee = buy_fee * (1 - matched_quantity / buy_quantity)
                        buy_queue[0] = (buy_price, buy_quantity - matched_quantity, remaining_fee)

            self.total_fees = total_fees
            return total_profit

        except Exception as e:
            logger.error(f"计算数据库利润时出错: {e}")
            import traceback
            traceback.print_exc()
            return 0
    
    def _update_trading_stats(self):
        """更新每日交易统计数据"""
        if not self._db_available():
            return
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            
            # 计算额外指标
            volatility = 0
            if self.ws and hasattr(self.ws, 'historical_prices'):
                volatility = calculate_volatility(self.ws.historical_prices)
            
            # 计算平均价差
            avg_spread = 0
            if self.ws and self.ws.bid_price and self.ws.ask_price:
                avg_spread = (self.ws.ask_price - self.ws.bid_price) / ((self.ws.ask_price + self.ws.bid_price) / 2) * 100
            
            # 准备统计数据
            stats_data = {
                'date': today,
                'symbol': self.symbol,
                'maker_buy_volume': self.maker_buy_volume,
                'maker_sell_volume': self.maker_sell_volume,
                'taker_buy_volume': self.taker_buy_volume,
                'taker_sell_volume': self.taker_sell_volume,
                'realized_profit': self.total_profit,
                'total_fees': self.total_fees,
                'net_profit': self.total_profit - self.total_fees,
                'avg_spread': avg_spread,
                'trade_count': self.trades_executed,
                'volatility': volatility
            }
            
            # 使用专门的函数来处理数据库操作
            def safe_update_stats():
                try:
                    success = self.db.update_trading_stats(stats_data)
                    if not success:
                        logger.warning("更新交易统计失败，下次再试")
                except Exception as db_err:
                    logger.error(f"更新交易统计时出错: {db_err}")
            
            # 直接在当前线程执行，避免过多的并发操作
            safe_update_stats()
                
        except Exception as e:
            logger.error(f"更新交易统计数据时出错: {e}")
            import traceback
            traceback.print_exc()
    
    def _calculate_average_buy_cost(self):
        """计算平均买入成本"""
        if not self.buy_trades:
            return 0
            
        total_buy_cost = sum(price * quantity for price, quantity in self.buy_trades)
        total_buy_quantity = sum(quantity for _, quantity in self.buy_trades)
        
        if not self.sell_trades or total_buy_quantity <= 0:
            return total_buy_cost / total_buy_quantity if total_buy_quantity > 0 else 0
        
        buy_queue = self.buy_trades.copy()
        consumed_cost = 0
        consumed_quantity = 0
        
        for _, sell_quantity in self.sell_trades:
            remaining_sell = sell_quantity
            
            while remaining_sell > 0 and buy_queue:
                buy_price, buy_quantity = buy_queue[0]
                matched_quantity = min(remaining_sell, buy_quantity)
                consumed_cost += buy_price * matched_quantity
                consumed_quantity += matched_quantity
                remaining_sell -= matched_quantity
                
                if matched_quantity >= buy_quantity:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (buy_price, buy_quantity - matched_quantity)
        
        remaining_buy_quantity = total_buy_quantity - consumed_quantity
        remaining_buy_cost = total_buy_cost - consumed_cost
        
        if remaining_buy_quantity <= 0:
            if self.ws and self.ws.connected and self.ws.bid_price:
                return self.ws.bid_price
            return 0
        
        return remaining_buy_cost / remaining_buy_quantity
    
    def _calculate_session_profit(self):
        """计算本次执行的已实现利润"""
        if not self.session_buy_trades or not self.session_sell_trades:
            return 0

        buy_queue = self.session_buy_trades.copy()
        total_profit = 0

        for sell_price, sell_quantity in self.session_sell_trades:
            remaining_sell = sell_quantity

            while remaining_sell > 0 and buy_queue:
                buy_price, buy_quantity = buy_queue[0]
                matched_quantity = min(remaining_sell, buy_quantity)

                # 计算这笔交易的利润
                trade_profit = (sell_price - buy_price) * matched_quantity
                total_profit += trade_profit

                remaining_sell -= matched_quantity
                if matched_quantity >= buy_quantity:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (buy_price, buy_quantity - matched_quantity)

        return total_profit

    def calculate_pnl(self):
        """计算已实现和未实现PnL"""
        # 总的已实现利润
        realized_pnl = self._calculate_db_profit()
        
        # 本次执行的已实现利润
        session_realized_pnl = self._calculate_session_profit()
        
        # 计算未实现利润
        unrealized_pnl = 0
        net_position = self.total_bought - self.total_sold
        
        if net_position > 0:
            current_price = self.get_current_price()
            if current_price:
                avg_buy_cost = self._calculate_average_buy_cost()
                unrealized_pnl = (current_price - avg_buy_cost) * net_position
        
        # 返回总的PnL和本次执行的PnL
        return realized_pnl, unrealized_pnl, self.total_fees, realized_pnl - self.total_fees, session_realized_pnl, self.session_fees, session_realized_pnl - self.session_fees
    
    def get_current_price(self):
        """获取当前价格（优先使用WebSocket数据）"""
        # 只检查连接状态，不触发重连（避免频繁重连尝试）
        price = None
        bid_price, ask_price = self.get_market_depth()
        price = (bid_price + ask_price) / 2
        return price
    
    def get_market_depth(self):
        """只通过API获取市场深度（最高买价和最低卖价）"""
        order_book = self.client.get_order_book(self.symbol)
        if isinstance(order_book, dict) and "error" in order_book:
            logger.error(f"获取订单簿失败: {order_book['error']}")
            return None, None

        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        if not bids or not asks:
            return None, None

        highest_bid = float(bids[0][0])
        lowest_ask = float(asks[0][0])

        return highest_bid, lowest_ask
    
    def calculate_dynamic_spread(self):
        """计算动态价差基于市场情况"""
        base_spread = self.base_spread_percentage
        
        # 返回基础价差，不再进行动态计算
        return base_spread
    
    def calculate_prices(self):
        """计算买卖订单价格"""
        try:
            bid_price, ask_price = self.get_market_depth()
            if bid_price is None or ask_price is None:
                logger.error("无法获取价格信息，无法设置订单")
                return None, None
            else:
                mid_price = (bid_price + ask_price) / 2
            
            logger.info(f"市场中间价: {mid_price}")
            
            # 使用基础价差
            spread_percentage = self.base_spread_percentage
            exact_spread = mid_price * (spread_percentage / 100)
            
            base_buy_price = mid_price - (exact_spread / 2)
            base_sell_price = mid_price + (exact_spread / 2)
            
            base_buy_price = round_to_tick_size(base_buy_price, self.tick_size)
            base_sell_price = round_to_tick_size(base_sell_price, self.tick_size)
            
            actual_spread = base_sell_price - base_buy_price
            actual_spread_pct = (actual_spread / mid_price) * 100
            logger.info(f"使用的价差: {actual_spread_pct:.4f}% (目标: {spread_percentage}%), 绝对价差: {actual_spread}")
            
            # 计算梯度订单价格
            buy_prices = []
            sell_prices = []
            
            # 优化梯度分布：较小的梯度以提高成交率
            for i in range(self.max_orders):
                # 非线性递增的梯度，靠近中间的订单梯度小，越远离中间梯度越大
                gradient_factor = (i ** 1.2) * 1.2
                
                buy_adjustment = gradient_factor * self.tick_size
                sell_adjustment = gradient_factor * self.tick_size
                
                buy_price = round_to_tick_size(base_buy_price - buy_adjustment, self.tick_size)
                sell_price = round_to_tick_size(base_sell_price + sell_adjustment, self.tick_size)
                
                buy_prices.append(buy_price)
                sell_prices.append(sell_price)
            
            final_spread = sell_prices[0] - buy_prices[0]
            final_spread_pct = (final_spread / mid_price) * 100
            logger.info(f"最终价差: {final_spread_pct:.4f}% (最低卖价 {sell_prices[0]} - 最高买价 {buy_prices[0]} = {final_spread})")
            
            return buy_prices, sell_prices
        
        except Exception as e:
            logger.error(f"计算价格时出错: {str(e)}")
            return None, None
    
    def need_rebalance(self):
        """判断是否需要重平衡仓位（基于总余额包含抵押品）"""
        # 检查重平功能是否开启
        if not self.enable_rebalance:
            logger.debug("重平功能已关闭，跳过重平衡检查")
            return False
            
        logger.info("检查是否需要重平衡仓位...")
        
        # 获取当前价格
        current_price = self.get_current_price()
        if not current_price:
            logger.warning("无法获取当前价格，跳过重平衡检查")
            return False
        
        # 获取基础资产和报价资产的总可用余额（包含抵押品）
        base_available, base_total = self.get_asset_balance(self.base_asset)
        quote_available, quote_total = self.get_asset_balance(self.quote_asset)
        
        logger.info(f"当前基础资产余额: 可用 {format_balance(base_available)} {self.base_asset}, 总计 {format_balance(base_total)} {self.base_asset}")
        logger.info(f"当前报价资产余额: 可用 {format_balance(quote_available)} {self.quote_asset}, 总计 {format_balance(quote_total)} {self.quote_asset}")
        
        # 计算总资产价值（以报价货币计算）
        total_assets = quote_total + (base_total * current_price)
        
        # 检查是否有足够资产进行重平衡
        min_asset_value = self.min_order_size * current_price * 10  # 最小资产要求
        if total_assets < min_asset_value:
            logger.info(f"总资产价值 {total_assets:.2f} {self.quote_asset} 过小，跳过重平衡检查")
            return False
        
        # 使用用户设定的目标比例
        ideal_base_value = total_assets * (self.base_asset_target_percentage / 100)
        actual_base_value = base_total * current_price
        
        # 计算偏差
        deviation_value = abs(actual_base_value - ideal_base_value)
        risk_exposure = (deviation_value / total_assets) * 100 if total_assets > 0 else 0
        
        logger.info(f"总资产价值: {total_assets:.2f} {self.quote_asset}")
        logger.info(f"目标配置比例: {self.base_asset_target_percentage}% {self.base_asset} / {self.quote_asset_target_percentage}% {self.quote_asset}")
        logger.info(f"理想基础资产价值: {ideal_base_value:.2f} {self.quote_asset}")
        logger.info(f"实际基础资产价值: {actual_base_value:.2f} {self.quote_asset}")
        logger.info(f"偏差: {deviation_value:.2f} {self.quote_asset}")
        logger.info(f"风险暴露比例: {risk_exposure:.2f}% (阈值: {self.rebalance_threshold}%)")
        
        need_rebalance = risk_exposure > self.rebalance_threshold
        logger.info(f"重平衡检查结果: {'需要重平衡' if need_rebalance else '不需要重平衡'}")
        
        return need_rebalance
    
    def rebalance_position(self):
        """重平衡仓位（使用总余额包含抵押品）"""
        # 检查重平功能是否开启
        if not self.enable_rebalance:
            logger.warning("重平功能已关闭，取消重平衡操作")
            return
            
        logger.info("开始重新平衡仓位...")
        self.check_ws_connection()
        
        # 获取当前价格
        current_price = self.get_current_price()
        if not current_price:
            logger.error("无法获取价格，无法重新平衡")
            return
        
        # 获取市场深度
        bid_price, ask_price = self.get_market_depth()
        if bid_price is None or ask_price is None:
            bid_price = current_price * 0.999
            ask_price = current_price * 1.001
        
        # 获取总可用余额（包含抵押品）
        base_available, base_total = self.get_asset_balance(self.base_asset)
        quote_available, quote_total = self.get_asset_balance(self.quote_asset)
        
        logger.info(f"基础资产: 可用 {format_balance(base_available)}, 总计 {format_balance(base_total)} {self.base_asset}")
        logger.info(f"报价资产: 可用 {format_balance(quote_available)}, 总计 {format_balance(quote_total)} {self.quote_asset}")
        
        # 计算总资产价值
        total_assets = quote_total + (base_total * current_price)
        ideal_base_value = total_assets * (self.base_asset_target_percentage / 100)
        actual_base_value = base_total * current_price
        
        logger.info(f"使用目标配置比例: {self.base_asset_target_percentage}% {self.base_asset} / {self.quote_asset_target_percentage}% {self.quote_asset}")
        
        # 判断需要买入还是卖出
        if actual_base_value > ideal_base_value:
            # 基础资产过多，需要卖出
            excess_value = actual_base_value - ideal_base_value
            quantity_to_sell = excess_value / current_price
            
            
            max_sellable = base_total * 0.95  # 保留5%作为缓冲，基于总余额
            quantity_to_sell = min(quantity_to_sell, max_sellable)
            quantity_to_sell = round_to_precision(quantity_to_sell, self.base_precision)
            
            if quantity_to_sell < self.min_order_size:
                logger.info(f"需要卖出的数量 {format_balance(quantity_to_sell)} 低于最小订单大小 {format_balance(self.min_order_size)}，不进行重新平衡")
                return
                
            
            if quantity_to_sell > base_total:
                logger.warning(f"需要卖出 {format_balance(quantity_to_sell)} 但总余额只有 {format_balance(base_total)}，调整为总余额的90%")
                quantity_to_sell = round_to_precision(base_total * 0.9, self.base_precision)
            
            # 检查可用余额，如果为0则依靠自动赎回
            if base_available < quantity_to_sell:
                logger.info(f"可用余额 {format_balance(base_available)} 不足，需要卖出 {format_balance(quantity_to_sell)}，将依靠自动赎回功能")
            
            # 使用略低于当前买价的价格来快速成交
            sell_price = round_to_tick_size(bid_price * 0.999, self.tick_size)
            logger.info(f"执行重新平衡: 卖出 {format_balance(quantity_to_sell)} {self.base_asset} @ {format_balance(sell_price)}")
            
            # 构建订单
            order_details = {
                "orderType": "Limit",
                "price": str(sell_price),
                "quantity": str(quantity_to_sell),
                "side": "Ask",
                "symbol": self.symbol,
                "timeInForce": "IOC",  # 立即成交或取消，避免挂单
                "autoLendRedeem": True,
                "autoLend": True
            }
            
        elif actual_base_value < ideal_base_value:
            # 基础资产不足，需要买入
            deficit_value = ideal_base_value - actual_base_value
            quantity_to_buy = deficit_value / current_price
            
            # 计算需要的报价资产
            cost = quantity_to_buy * ask_price
            max_affordable_cost = quote_total * 0.95  # 基于总余额的95%
            max_affordable = max_affordable_cost / ask_price
            quantity_to_buy = min(quantity_to_buy, max_affordable)
            quantity_to_buy = round_to_precision(quantity_to_buy, self.base_precision)
            
            if quantity_to_buy < self.min_order_size:
                logger.info(f"需要买入的数量 {format_balance(quantity_to_buy)} 低于最小订单大小 {format_balance(self.min_order_size)}，不进行重新平衡")
                return
                
            cost = quantity_to_buy * ask_price
            if cost > quote_total:
                logger.warning(f"需要 {format_balance(cost)} {self.quote_asset} 但总余额只有 {format_balance(quote_total)}，调整买入数量")
                quantity_to_buy = round_to_precision((quote_total * 0.9) / ask_price, self.base_precision)
                cost = quantity_to_buy * ask_price
            
            # 检查可用余额
            if quote_available < cost:
                logger.info(f"可用余额 {format_balance(quote_available)} {self.quote_asset} 不足，需要 {format_balance(cost)} {self.quote_asset}，将依靠自动赎回功能")
            
            # 使用略高于当前卖价的价格来快速成交
            buy_price = round_to_tick_size(ask_price * 1.001, self.tick_size)
            logger.info(f"执行重新平衡: 买入 {format_balance(quantity_to_buy)} {self.base_asset} @ {format_balance(buy_price)}")
            
            # 构建订单
            order_details = {
                "orderType": "Limit",
                "price": str(buy_price),
                "quantity": str(quantity_to_buy),
                "side": "Bid",
                "symbol": self.symbol,
                "timeInForce": "IOC",  # 立即成交或取消，避免挂单
                "autoLendRedeem": True,
                "autoLend": True
            }
        else:
            logger.info("仓位已经均衡，无需重新平衡")
            return
        
        # 执行订单
        result = self.client.execute_order(order_details)
        
        if isinstance(result, dict) and "error" in result:
            logger.error(f"重新平衡订单执行失败: {result['error']}")
        else:
            logger.info(f"重新平衡订单执行成功")
            # 记录这是一个重平衡订单
            if 'id' in result and self._db_available():
                self.db.record_rebalance_order(result['id'], self.symbol)
        
        logger.info("仓位重新平衡完成")
    
    def subscribe_order_updates(self):
        """订阅订单更新流"""
        if not self.ws or not self.ws.is_connected():
            logger.warning("无法订阅订单更新：WebSocket连接不可用")
            return False
        
        # 尝试订阅订单更新流
        stream = f"account.orderUpdate.{self.symbol}"
        if stream not in self.ws.subscriptions:
            retry_count = 0
            max_retries = 3
            success = False
            
            while retry_count < max_retries and not success:
                try:
                    success = self.ws.private_subscribe(stream)
                    if success:
                        logger.info(f"成功订阅订单更新: {stream}")
                        return True
                    else:
                        logger.warning(f"订阅订单更新失败，尝试重试... ({retry_count+1}/{max_retries})")
                except Exception as e:
                    logger.error(f"订阅订单更新时发生异常: {e}")
                
                retry_count += 1
                if retry_count < max_retries:
                    time.sleep(1)  # 重试前等待
            
            if not success:
                logger.error(f"在 {max_retries} 次尝试后仍无法订阅订单更新")
                return False
        else:
            logger.info(f"已经订阅了订单更新: {stream}")
            return True
    
    def place_limit_orders(self):
        """下限价单（使用总余额包含抵押品）"""
        self.check_ws_connection()
        self.cancel_existing_orders()
        
        buy_prices, sell_prices = self.calculate_prices()
        if buy_prices is None or sell_prices is None:
            logger.error("无法计算订单价格，跳过分单")
            return
        
        # 处理订单数量
        if self.order_quantity is None:
            # 获取总可用余额（包含抵押品）
            base_available, base_total = self.get_asset_balance(self.base_asset)
            quote_available, quote_total = self.get_asset_balance(self.quote_asset)
            
            logger.info(f"当前总余额: {format_balance(base_total)} {self.base_asset}, {format_balance(quote_total)} {self.quote_asset}")
            logger.info(f"当前可用余额: {format_balance(base_available)} {self.base_asset}, {format_balance(quote_available)} {self.quote_asset}")
            
            # 如果可用余额很少但总余额充足，说明资金在抵押品中
            if base_available < base_total * 0.1:
                logger.info(f"基础资产主要在抵押品中，将依靠自动赎回功能")
            if quote_available < quote_total * 0.1:
                logger.info(f"报价资产主要在抵押品中，将依靠自动赎回功能")
            
            # 计算每个订单的数量
            avg_price = sum(buy_prices) / len(buy_prices)
            
            # 使用更保守的分配比例，避免资金用尽
            allocation_percent = min(0.05, 1.0 / (self.max_orders * 4))  # 最多使用总资金的25%
            
            # 基于总余额计算，而不是可用余额
            quote_amount_per_side = quote_total * allocation_percent
            base_amount_per_side = base_total * allocation_percent
            
            buy_quantity = max(self.min_order_size, round_to_precision(quote_amount_per_side / avg_price, self.base_precision))
            sell_quantity = max(self.min_order_size, round_to_precision(base_amount_per_side, self.base_precision))
            
            logger.info(f"计算订单数量: 买单 {format_balance(buy_quantity)} {self.base_asset}, 卖单 {format_balance(sell_quantity)} {self.base_asset}")
        else:
            buy_quantity = max(self.min_order_size, round_to_precision(self.order_quantity, self.base_precision))
            sell_quantity = max(self.min_order_size, round_to_precision(self.order_quantity, self.base_precision))
        
        # 下买单 (并发处理)
        buy_futures = []

        def place_buy(price, qty):
            order = {
                "orderType": "Limit",
                "price": str(price),
                "quantity": str(qty),
                "side": "Bid",
                "symbol": self.symbol,
                "timeInForce": "GTC",
                "postOnly": True,
                "autoLendRedeem": True,
                "autoLend": True
            }
            
            max_retries = 6
            retries = 0
            while retries < max_retries:
                res = self.client.execute_order(order)
                if not (isinstance(res, dict) and "error" in res and "take" in str(res["error"])):
                    break  # 成功或非 "take" 错误，跳出循环
        
                # 计算逐渐增大的调整幅度
                adjustment = self.tick_size * 2 * (retries + 1)
                logger.info(f"调整买单价格并重试... (尝试 {retries+1}/{max_retries}, 调整幅度: -{adjustment})")
                order["price"] = str(round_to_tick_size(float(order["price"]) - adjustment, self.tick_size))
                retries += 1
                time.sleep(0.1)  # 添加延迟，避免 API 频率限制
            
            # 特殊处理资金不足错误
            if isinstance(res, dict) and "error" in res and "INSUFFICIENT_FUNDS" in str(res["error"]):
                logger.warning(f"买单资金不足，可能需要手动赎回抵押品或等待自动赎回生效")
            
            return qty, order["price"], res

        with ThreadPoolExecutor(max_workers=self.max_orders) as executor:
            for p in buy_prices:
                if len(buy_futures) >= self.max_orders:
                    break
                buy_futures.append(executor.submit(place_buy, p, buy_quantity))

        buy_order_count = 0
        for future in buy_futures:
            qty, p_used, res = future.result()
            if isinstance(res, dict) and "error" in res:
                logger.error(f"买单失败: {res['error']}")
            else:
                logger.info(f"买单成功: 价格 {p_used}, 数量 {qty}")
                self.active_buy_orders.append(res)
                self.orders_placed += 1
                buy_order_count += 1

        # 下卖单
        sell_futures = []

        def place_sell(price, qty):
            order = {
                "orderType": "Limit",
                "price": str(price),
                "quantity": str(qty),
                "side": "Ask",
                "symbol": self.symbol,
                "timeInForce": "GTC",
                "postOnly": True,
                "autoLendRedeem": True,
                "autoLend": True
            }

            max_retries = 6
            retries = 0
            while retries < max_retries:
                res = self.client.execute_order(order)
                if not (isinstance(res, dict) and "error" in res and "take" in str(res["error"])):
                    break  # 成功或非 "take" 错误，跳出循环
        
                # 计算逐渐增大的调整幅度
                adjustment = self.tick_size * 2 * (retries + 1)
                logger.info(f"调整卖单价格并重试... (尝试 {retries+1}/{max_retries}, 调整幅度: +{adjustment})")
                order["price"] = str(round_to_tick_size(float(order["price"]) + adjustment, self.tick_size))
                retries += 1
                time.sleep(0.1)  # 添加延迟，避免 API 频率限制
            
            # 特殊处理资金不足错误
            if isinstance(res, dict) and "error" in res and "INSUFFICIENT_FUNDS" in str(res["error"]):
                logger.warning(f"卖单资金不足，可能需要手动赎回抵押品或等待自动赎回生效")
            
            return qty, order["price"], res

        with ThreadPoolExecutor(max_workers=self.max_orders) as executor:
            for p in sell_prices:
                if len(sell_futures) >= self.max_orders:
                    break
                sell_futures.append(executor.submit(place_sell, p, sell_quantity))

        sell_order_count = 0
        for future in sell_futures:
            qty, p_used, res = future.result()
            if isinstance(res, dict) and "error" in res:
                logger.error(f"卖单失败: {res['error']}")
            else:
                logger.info(f"卖单成功: 价格 {p_used}, 数量 {qty}")
                self.active_sell_orders.append(res)
                self.orders_placed += 1
                sell_order_count += 1
            
        logger.info(f"共分单: {buy_order_count} 个买单, {sell_order_count} 个卖单")
    
    def cancel_existing_orders(self):
        """取消所有现有订单"""
        open_orders = self.client.get_open_orders(self.symbol)
        
        if isinstance(open_orders, dict) and "error" in open_orders:
            logger.error(f"获取订单失败: {open_orders['error']}")
            return
        
        if not open_orders:
            logger.info("没有需要取消的现有订单")
            self.active_buy_orders = []
            self.active_sell_orders = []
            return
        
        logger.info(f"正在取消 {len(open_orders)} 个现有订单")
        
        try:
            # 尝试批量取消
            result = self.client.cancel_all_orders(self.symbol)
            
            if isinstance(result, dict) and "error" in result:
                logger.error(f"批量取消订单失败: {result['error']}")
                logger.info("尝试逐个取消...")
                
                # 初始化线程池
                with ThreadPoolExecutor(max_workers=5) as executor:
                    cancel_futures = []
                    
                    # 提交取消订单任务
                    for order in open_orders:
                        order_id = order.get('id')
                        if not order_id:
                            continue
                        
                        # Use legacy wrapper to keep existing logic; could be refactored to self.client.cancel_order
                        # Directly use instance client method now
                        future = executor.submit(
                            self.client.cancel_order,
                            order_id,
                            self.symbol
                        )
                        cancel_futures.append((order_id, future))
                    
                    # 处理结果
                    for order_id, future in cancel_futures:
                        try:
                            res = future.result()
                            if isinstance(res, dict) and "error" in res:
                                logger.error(f"取消订单 {order_id} 失败: {res['error']}")
                            else:
                                logger.info(f"取消订单 {order_id} 成功")
                                self.orders_cancelled += 1
                        except Exception as e:
                            logger.error(f"取消订单 {order_id} 时出错: {e}")
            else:
                logger.info("批量取消订单成功")
                self.orders_cancelled += len(open_orders)
        except Exception as e:
            logger.error(f"取消订单过程中发生错误: {str(e)}")
        
        # 等待一下确保订单已取消
        time.sleep(1)
        
        # 检查是否还有未取消的订单
        remaining_orders = self.client.get_open_orders(self.symbol)
        if remaining_orders and len(remaining_orders) > 0:
            logger.warning(f"警告: 仍有 {len(remaining_orders)} 个未取消的订单")
        else:
            logger.info("所有订单已成功取消")
        
        # 重置活跃订单列表
        self.active_buy_orders = []
        self.active_sell_orders = []
    
    def check_order_fills(self):
        open_orders = self.client.get_open_orders(self.symbol)
        if isinstance(open_orders, dict) and "error" in open_orders:
            logger.error(f"获取订单失败: {open_orders['error']}")
            return []
        current_order_ids = set()
        if open_orders:
            for order in open_orders:
                order_id = order.get('id')
                if order_id:
                    current_order_ids.add(order_id)
        prev_buy_orders = len(self.active_buy_orders)
        prev_sell_orders = len(self.active_sell_orders)
        filled_order_ids = []
        for order in self.active_buy_orders + self.active_sell_orders:
            order_id = order.get('id')
            if order_id and order_id not in current_order_ids:
                filled_order_ids.append(order_id)
        filled_trades = []
        if filled_order_ids:
            try:
                recent_fills = self.client.get_fill_history(self.symbol, limit=50)
                if recent_fills and not (isinstance(recent_fills, dict) and "error" in recent_fills):
                    if not hasattr(self, '_processed_fill_ids'):
                        self._processed_fill_ids = set()
                    for fill in recent_fills:
                        fill_id = fill.get('id')
                        fill_order_id = fill.get('order_id')
                        if fill_id in self._processed_fill_ids:
                            continue
                        if fill_order_id in filled_order_ids:
                            filled_trades.append(fill)
                            self._processed_fill_ids.add(fill_id)
                            side = fill.get('side', '').upper()
                            price = float(fill.get('price', 0))
                            size = float(fill.get('size', 0))
                            liquidity = fill.get('liquidity', 'UNKNOWN')
                            realized_pnl = fill.get('realized_pnl', 0)
                            is_maker = liquidity.upper() == 'MAKER'
                            
                            # 获取手续费信息
                            fee = float(fill.get('fee', 0))
                            fee_currency = fill.get('fee_currency', self.quote_asset)
                            
                            # 构建完整的成交资讯
                            fill_info = {
                                'side': 'Bid' if side == 'BUY' else 'Ask',
                                'quantity': size,
                                'price': price,
                                'maker': is_maker,
                                'order_id': fill.get('order_id'),
                                'trade_id': fill.get('id'),
                                'realized_pnl': realized_pnl,
                                'fee': fee,
                                'fee_currency': fee_currency
                            }
                            
                            logger.info(
                                f"✓ {'买' if side == 'BUY' else '卖'}单成交 ({liquidity}): "
                                f"{size} @ {price}, 已实现盈亏: {realized_pnl}, 手续费: {fee} {fee_currency}"
                            )
                            
                            # 触发成交后处理
                            self._process_order_fill_event(
                                side=fill_info['side'],
                                quantity=fill_info['quantity'],
                                price=fill_info['price'],
                                order_id=fill_info['order_id'],
                                maker=fill_info['maker'],
                                fee=fee,
                                fee_asset=fee_currency,
                                trade_id=fill_info['trade_id'],
                                source='rest',
                                timestamp=int(time.time() * 1000)
                            )
            except Exception as e:
                logger.error(f"获取成交记录失败: {e}")
        active_buy_orders = []
        active_sell_orders = []
        if open_orders:
            for order in open_orders:
                if order.get('side') == 'Bid' or order.get('side') == 'BUY':
                    active_buy_orders.append(order)
                elif order.get('side') == 'Ask' or order.get('side') == 'SELL':
                    active_sell_orders.append(order)
        self.active_buy_orders = active_buy_orders
        self.active_sell_orders = active_sell_orders
        if prev_buy_orders != len(active_buy_orders) or prev_sell_orders != len(active_sell_orders):
            logger.info(f"订单数量变更: 买单 {prev_buy_orders} -> {len(active_buy_orders)}, 卖单 {prev_sell_orders} -> {len(active_sell_orders)}")
        logger.info(f"当前活跃订单: 买单 {len(self.active_buy_orders)} 个, 卖单 {len(self.active_sell_orders)} 个")
        return filled_trades
    def estimate_profit(self, pnl_data=None):
        """输出本次迭代的关键统计资讯。"""
        if pnl_data is None:
            pnl_data = self.calculate_pnl()

        (
            realized_pnl,
            unrealized_pnl,
            total_fees,
            net_pnl,
            session_realized_pnl,
            session_fees,
            session_net_pnl,
        ) = pnl_data

        session_buy_volume = sum(qty for _, qty in self.session_buy_trades)
        session_sell_volume = sum(qty for _, qty in self.session_sell_trades)

        sections: List[Tuple[str, List[Union[str, Tuple[str, str]]]]] = []

        if session_buy_volume > 0 or session_sell_volume > 0:
            session_rows: List[Union[str, Tuple[str, str]]] = [
                ("成交量", f"买入 {session_buy_volume:.3f} {self.base_asset} | 卖出 {session_sell_volume:.3f} {self.base_asset}"),
                (
                    "盈亏",
                    f"已实现 {session_realized_pnl:.4f} {self.quote_asset} | 净利润 {session_net_pnl:.4f} {self.quote_asset} (手续费 {session_fees:.4f} {self.quote_asset})",
                ),
                ("Maker成交量", f"买 {self.session_maker_buy_volume:.3f} {self.base_asset} | 卖 {self.session_maker_sell_volume:.3f} {self.base_asset}"),
                ("Taker成交量", f"买 {self.session_taker_buy_volume:.3f} {self.base_asset} | 卖 {self.session_taker_sell_volume:.3f} {self.base_asset}"),
            ]
            session_rows.insert(1, ("成交额", f"{self.session_quote_volume:.2f} {self.quote_asset}"))
        else:
            session_rows = [
                "本次迭代没有成交记录",
                (
                    "盈亏",
                    f"已实现 {session_realized_pnl:.4f} {self.quote_asset} | 净利润 {session_net_pnl:.4f} {self.quote_asset} (手续费 {session_fees:.4f} {self.quote_asset})",
                ),
                ("Maker成交量", f"买 {self.session_maker_buy_volume:.3f} {self.base_asset} | 卖 {self.session_maker_sell_volume:.3f} {self.base_asset}"),
                ("Taker成交量", f"买 {self.session_taker_buy_volume:.3f} {self.base_asset} | 卖 {self.session_taker_sell_volume:.3f} {self.base_asset}"),
            ]

        sections.append(("本次执行", session_rows))

        sections.append(
            (
                "累计表现",
                [
                    ("累计盈亏", f"{net_pnl:.4f} {self.quote_asset}"),
                    ("未实现盈亏", f"{unrealized_pnl:.4f} {self.quote_asset}"),
                    ("累计手续费", f"{total_fees:.4f} {self.quote_asset}"),
                ],
            )
        )

        sections.append(
            (
                "交易计数",
                [
                    ("成交次数", f"{self.trades_executed} 次"),
                    ("分单次数", f"{self.orders_placed} 次"),
                    ("取消次数", f"{self.orders_cancelled} 次"),
                ],
            )
        )

        if self.total_quote_volume > 0:
            loss = min(net_pnl, 0)  # 仅取亏损
            wear_rate_value = abs(loss) / self.total_quote_volume * 100
            wear_rate_display = f"{wear_rate_value:.4f}%"
        else:
            wear_rate_display = "N/A"


        trade_rows = [
            ("总成交量", f"买 {self.total_bought:.3f} {self.base_asset} | 卖 {self.total_sold:.3f} {self.base_asset}"),
            ("总成交额", f"{self.total_quote_volume:.2f} {self.quote_asset}"),
            ("Maker总量", f"买 {self.maker_buy_volume:.3f} {self.base_asset} | 卖 {self.maker_sell_volume:.3f} {self.base_asset}"),
            ("Taker总量", f"买 {self.taker_buy_volume:.3f} {self.base_asset} | 卖 {self.taker_sell_volume:.3f} {self.base_asset}"),
            ("磨损率", wear_rate_display),
        ]

        sections.append(
            (
                "成交概况",
                trade_rows,
            )
        )

        if self.active_buy_orders and self.active_sell_orders:
            buy_price = float(self.active_buy_orders[-1].get('price', 0))
            sell_price = float(self.active_sell_orders[0].get('price', 0))
            spread = sell_price - buy_price
            spread_pct = (spread / buy_price * 100) if buy_price > 0 else 0
            order_line = f"买 {buy_price:.3f} | 卖 {sell_price:.3f} | 价差 {spread:.3f} ({spread_pct:.3f}%)"
        else:
            active_buy_count = len(self.active_buy_orders)
            active_sell_count = len(self.active_sell_orders)
            order_line = f"买单 {active_buy_count} | 卖单 {active_sell_count}"

        sections.append(
            (
                "市场状态",
                [
                    ("活跃订单", order_line),
                    ("WebSocket状态", "已连接" if self.ws and self.ws.is_connected() else "未连接"),
                ],
            )
        )

        extra_sections = self._get_extra_summary_sections()
        if extra_sections:
            sections.extend(extra_sections)

        self._log_boxed_summary("做市统计总结", sections)

    def _get_extra_summary_sections(self) -> List[Tuple[str, List[Union[str, Tuple[str, str]]]]]:
        """提供子类扩展的统计输出。"""
        return []

    def _log_boxed_summary(self, title: str, sections: List[Tuple[str, List[Union[str, Tuple[str, str]]]]]):
        """以框线格式输出统计资讯。"""
        inner_width = 74
        border_top = f"┌{'─' * inner_width}┐"
        border_section = f"├{'─' * inner_width}┤"
        border_bottom = f"└{'─' * inner_width}┘"

        logger.info(border_top)
        self._log_box_text(title, inner_width, align="center")

        for index, (section_title, rows) in enumerate(sections):
            logger.info(border_section)
            self._log_box_text(f"▸ {section_title}", inner_width)

            for row in rows:
                if isinstance(row, tuple):
                    label, value = row
                    self._log_box_key_value(label, value, inner_width)
                else:
                    self._log_box_text(str(row), inner_width)

        logger.info(border_bottom)

    def _log_box_text(self, text: str, inner_width: int, align: str = "left"):
        """在框线内输出单行或多行文字。"""
        if align == "center":
            logger.info(f"│ {self._center_display(text, inner_width)} │")
            return

        for line in self._wrap_display_text(text, inner_width):
            logger.info(f"│ {self._pad_display(line, inner_width)} │")

    def _log_box_key_value(self, label: str, value: str, inner_width: int):
        """以键值形式输出内容，并处理换行。"""
        label_display = f"{label}："
        label_width = 18
        label_field = self._pad_display(label_display, label_width)
        value_width = max(10, inner_width - label_width - 1)
        empty_label = self._pad_display("", label_width)

        wrapped_values = self._wrap_display_text(value, value_width)
        for index, chunk in enumerate(wrapped_values):
            chunk_field = self._pad_display(chunk, value_width)
            if index == 0:
                line = f"{label_field} {chunk_field}"
            else:
                line = f"{empty_label} {chunk_field}"
            logger.info(f"│ {line} │")

    def _display_width(self, text: str) -> int:
        """计算字符串的可视宽度，处理全形与半形字符。"""
        width = 0
        for char in text:
            east_asian_width = unicodedata.east_asian_width(char)
            if east_asian_width in ("F", "W", "A"):
                width += 2
            else:
                width += 1
        return width

    def _pad_display(self, text: str, width: int) -> str:
        """将字符串填充至指定的显示宽度。"""
        padding = max(0, width - self._display_width(text))
        return f"{text}{' ' * padding}"

    def _center_display(self, text: str, width: int) -> str:
        """以显示宽度为基准进行置中。"""
        text_width = self._display_width(text)
        if text_width >= width:
            return text
        total_padding = width - text_width
        left = total_padding // 2
        right = total_padding - left
        return f"{' ' * left}{text}{' ' * right}"

    def _wrap_display_text(self, text: str, width: int) -> List[str]:
        """根据显示宽度换行。"""
        if not text:
            return [""]

        lines: List[str] = []
        current = ""
        current_width = 0

        for char in text:
            char_width = self._display_width(char)
            if current and current_width + char_width > width:
                lines.append(current)
                current = char
                current_width = char_width
            else:
                current += char
                current_width += char_width

        if current:
            lines.append(current)
        else:
            lines.append("")

        return lines
    
    def print_trading_stats(self):
        """打印交易统计报表"""
        try:
            logger.info("\n=== 做市商交易统计 ===")
            logger.info(f"交易对: {self.symbol}")

            today = datetime.now().strftime('%Y-%m-%d')
            if self._db_available():
                # 获取今天的统计数据
                today_stats = self.db.get_trading_stats(self.symbol, today)

                if today_stats and len(today_stats) > 0:
                    stat = today_stats[0]
                    maker_buy = stat['maker_buy_volume']
                    maker_sell = stat['maker_sell_volume']
                    taker_buy = stat['taker_buy_volume']
                    taker_sell = stat['taker_sell_volume']
                    profit = stat['realized_profit']
                    fees = stat['total_fees']
                    net = stat['net_profit']
                    avg_spread = stat['avg_spread']
                    volatility = stat['volatility']

                    total_volume = maker_buy + maker_sell + taker_buy + taker_sell
                    maker_percentage = ((maker_buy + maker_sell) / total_volume * 100) if total_volume > 0 else 0

                    logger.info(f"\n今日统计 ({today}):")
                    logger.info(f"Maker买入量: {maker_buy} {self.base_asset}")
                    logger.info(f"Maker卖出量: {maker_sell} {self.base_asset}")
                    logger.info(f"Taker买入量: {taker_buy} {self.base_asset}")
                    logger.info(f"Taker卖出量: {taker_sell} {self.base_asset}")
                    logger.info(f"总成交量: {total_volume} {self.base_asset}")
                    logger.info(f"Maker占比: {maker_percentage:.2f}%")
                    logger.info(f"平均价差: {avg_spread:.4f}%")
                    logger.info(f"波动率: {volatility:.4f}%")
                    logger.info(f"毛利润: {profit:.8f} {self.quote_asset}")
                    logger.info(f"总手续费: {fees:.8f} {self.quote_asset}")
                    logger.info(f"净利润: {net:.8f} {self.quote_asset}")

                # 获取所有时间的总计
                all_time_stats = self.db.get_all_time_stats(self.symbol)

                if all_time_stats:
                    total_maker_buy = all_time_stats['total_maker_buy']
                    total_maker_sell = all_time_stats['total_maker_sell']
                    total_taker_buy = all_time_stats['total_taker_buy']
                    total_taker_sell = all_time_stats['total_taker_sell']
                    total_profit = all_time_stats['total_profit']
                    total_fees = all_time_stats['total_fees']
                    total_net = all_time_stats['total_net_profit']
                    avg_spread = all_time_stats['avg_spread_all_time']

                    total_volume = total_maker_buy + total_maker_sell + total_taker_buy + total_taker_sell
                    maker_percentage = ((total_maker_buy + total_maker_sell) / total_volume * 100) if total_volume > 0 else 0

                    logger.info(f"\n累计统计:")
                    logger.info(f"Maker买入量: {total_maker_buy} {self.base_asset}")
                    logger.info(f"Maker卖出量: {total_maker_sell} {self.base_asset}")
                    logger.info(f"Taker买入量: {total_taker_buy} {self.base_asset}")
                    logger.info(f"Taker卖出量: {total_taker_sell} {self.base_asset}")
                    logger.info(f"总成交量: {total_volume} {self.base_asset}")
                    logger.info(f"Maker占比: {maker_percentage:.2f}%")
                    logger.info(f"平均价差: {avg_spread:.4f}%")
                    logger.info(f"毛利润: {total_profit:.8f} {self.quote_asset}")
                    logger.info(f"总手续费: {total_fees:.8f} {self.quote_asset}")
                    logger.info(f"净利润: {total_net:.8f} {self.quote_asset}")
            else:
                logger.info("数据库功能未启用，仅显示本次执行的统计资讯。")
            
            # 添加本次执行的统计
            session_buy_volume = sum(qty for _, qty in self.session_buy_trades)
            session_sell_volume = sum(qty for _, qty in self.session_sell_trades)
            session_total_volume = session_buy_volume + session_sell_volume
            session_maker_volume = self.session_maker_buy_volume + self.session_maker_sell_volume
            session_maker_percentage = (session_maker_volume / session_total_volume * 100) if session_total_volume > 0 else 0
            session_profit = self._calculate_session_profit()
            
            logger.info(f"\n本次执行统计 (从 {self.session_start_time.strftime('%Y-%m-%d %H:%M:%S')} 开始):")
            logger.info(f"Maker买入量: {self.session_maker_buy_volume} {self.base_asset}")
            logger.info(f"Maker卖出量: {self.session_maker_sell_volume} {self.base_asset}")
            logger.info(f"Taker买入量: {self.session_taker_buy_volume} {self.base_asset}")
            logger.info(f"Taker卖出量: {self.session_taker_sell_volume} {self.base_asset}")
            logger.info(f"总成交量: {session_total_volume} {self.base_asset}")
            logger.info(f"Maker占比: {session_maker_percentage:.2f}%")
            logger.info(f"毛利润: {session_profit:.8f} {self.quote_asset}")
            logger.info(f"总手续费: {self.session_fees:.8f} {self.quote_asset}")
            logger.info(f"净利润: {(session_profit - self.session_fees):.8f} {self.quote_asset}")
            
            # 添加重平设置信息
            logger.info(f"\n重平设置:")
            logger.info(f"重平功能: {'开启' if self.enable_rebalance else '关闭'}")
            if self.enable_rebalance:
                logger.info(f"目标比例: {self.base_asset_target_percentage}% {self.base_asset} / {self.quote_asset_target_percentage}% {self.quote_asset}")
                logger.info(f"触发阈值: {self.rebalance_threshold}%")
                
            # 查询前10笔最新成交
            if self._db_available():
                recent_trades = self.db.get_recent_trades(self.symbol, 10)

                if recent_trades and len(recent_trades) > 0:
                    logger.info("\n最近10笔成交:")
                    for i, trade in enumerate(recent_trades):
                        maker_str = "Maker" if trade['maker'] else "Taker"
                        logger.info(f"{i+1}. {trade['timestamp']} - {trade['side']} {trade['quantity']} @ {trade['price']} ({maker_str}) 手续费: {trade['fee']:.8f}")

        except Exception as e:
            logger.error(f"打印交易统计时出错: {e}")
    
    def _ensure_data_streams(self):
        """确保所有必要的数据流订阅都是活跃的"""
        # 如果使用 Websea，不需要 WebSocket 数据流
        if self.ws is None:
            return
            
        # 检查深度流订阅
        if "depth" not in self.ws.subscriptions:
            logger.info("重新订阅深度数据流...")
            self.ws.initialize_orderbook()  # 重新初始化订单簿
            self.ws.subscribe_depth()
        
        # 检查行情数据订阅
        if "bookTicker" not in self.ws.subscriptions:
            logger.info("重新订阅行情数据...")
            self.ws.subscribe_bookTicker()
        
        # 检查私有订单更新流
        if f"account.orderUpdate.{self.symbol}" not in self.ws.subscriptions:
            logger.info("重新订阅私有订单更新流...")
            self.subscribe_order_updates()

    def check_stop_conditions(self, realized_pnl, unrealized_pnl, session_realized_pnl) -> bool:
        """检查是否触发提前停止条件。

        基类默认不启用任何风控条件，返回 ``False``。

        Args:
            realized_pnl (float): 累计已实现盈亏。
            unrealized_pnl (float): 未实现盈亏。
            session_realized_pnl (float): 本次执行的已实现盈亏。

        Returns:
            bool: 是否应该提前停止策略。
        """

        return False

    def stop(self):
        """停止做市策略"""
        logger.info("收到停止信号，正在停止做市策略...")
        self._stop_flag = True
    
    def _price_deviation_exceeds_spread(self, current_price: float) -> bool:
        """检查价格偏离是否超过阈值"""
        if self.last_adjust_price is None:
            return True  # 首次调整
        deviation_pct = abs((current_price - self.last_adjust_price) / self.last_adjust_price) * 100
        logger.debug(f"当前价格: {current_price}, 上次调整价格: {self.last_adjust_price}, 偏离: {deviation_pct:.2f}%")
        return deviation_pct > self.force_adjust_spread

    def run(self, duration_seconds=3600, interval_seconds=60):
        """执行做市策略"""
        logger.info(f"开始运行做市策略: {self.symbol}")
        logger.info(f"运行时间: {duration_seconds} 秒, 间隔: {interval_seconds} 秒")
        
        # 打印重平设置
        logger.info(f"重平功能: {'开启' if self.enable_rebalance else '关闭'}")
        if self.enable_rebalance:
            logger.info(f"重平目标比例: {self.base_asset_target_percentage}% {self.base_asset} / {self.quote_asset_target_percentage}% {self.quote_asset}")
            logger.info(f"重平触发阈值: {self.rebalance_threshold}%")
        
        # 重置本次执行的统计数据
        self.session_start_time = datetime.now()
        self.session_buy_trades = []
        self.session_sell_trades = []
        self.session_fees = 0.0
        self.session_maker_buy_volume = 0.0
        self.session_maker_sell_volume = 0.0
        self.session_taker_buy_volume = 0.0
        self.session_taker_sell_volume = 0.0
        
        start_time = time.time()
        iteration = 0
        last_report_time = start_time
        report_interval = 300  # 5分钟打印一次报表
        
        try:
            # 先确保 WebSocket 连接可用
            connection_status = self.check_ws_connection()
            if connection_status and self.ws is not None:
                # 初始化订单簿和数据流
                if not self.ws.orderbook["bids"] and not self.ws.orderbook["asks"]:
                    self.ws.initialize_orderbook()
                
                # 检查并确保所有数据流订阅
                if "depth" not in self.ws.subscriptions:
                    self.ws.subscribe_depth()
                if "bookTicker" not in self.ws.subscriptions:
                    self.ws.subscribe_bookTicker()
                if f"account.orderUpdate.{self.symbol}" not in self.ws.subscriptions:
                    self.subscribe_order_updates()

            while time.time() - start_time < duration_seconds and not self._stop_flag:
                iteration += 1
                current_time = time.time()
                logger.info(f"\n=== 第 {iteration} 次迭代 ===")
                logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                td = datetime.now() - self.session_start_time
                total_seconds = int(td.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                seconds = total_seconds % 60
                run_time = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                logger.info(f"累计运行时间: {run_time}")
            
                # 检查连接并在必要时重连
                connection_status = self.check_ws_connection()
            
                # 如果连接成功，检查并确保所有流订阅
                if connection_status:
                    # 重新订阅必要的数据流
                    self._ensure_data_streams()
            
                # 检查订单成交情况
                self.check_order_fills()
            
                # 透过 REST API 同步最新成交
                if self.exchange in ('aster', 'lighter'):
                    self._sync_fill_history()
            
                # 检查是否需要重平衡仓位
                if self.need_rebalance():
                    self.rebalance_position()
              
                current_price = self.get_current_price()

                # === 统一触发逻辑：价格偏离 OR 成交触发 ===
                price_deviated = self._price_deviation_exceeds_spread(current_price)

                trade_triggered = False
                trigger_reason = ""

                if self.wait_all_filled:
                    # 新逻辑：只有全部挂单被吃完才触发
                    trade_triggered = (len(self.active_buy_orders) == 0 and len(self.active_sell_orders) == 0)
                    if trade_triggered:
                        trigger_reason = "所有订单已全部成交"
                else:
                    # 旧逻辑：只要有新成交就触发
                    current_count = self.trades_executed
                    if current_count > self.last_trades_count:
                        trade_triggered = True
                        trigger_reason = "检测到新成交"
                        self.last_trades_count = current_count  # 更新计数

                if trade_triggered or price_deviated:
                    if trade_triggered and price_deviated:
                        logger.info(f"{trigger_reason}，且价格偏离 {self.force_adjust_spread:.4f}% ，执行订单调整")
                    elif trade_triggered:
                        logger.info(f"{trigger_reason}，执行订单调整")
                    else:
                        logger.info(f"价格偏离超过 {self.force_adjust_spread:.4f}% ，执行订单调整")

                    self.place_limit_orders()          # 取消旧单 + 重新挂单
                    self.last_adjust_price = current_price
                else:
                    logger.info("未触发成交条件且价格偏离不足），维持订单不变")      
            
                # 计算PnL并输出简化统计
                pnl_data = self.calculate_pnl()
                self.estimate_profit(pnl_data)
            
                # 定期打印交易统计报表
                if current_time - last_report_time >= report_interval:
                    self.print_trading_stats()
                    last_report_time = current_time
            
                (
                    realized_pnl,
                    unrealized_pnl,
                    _total_fees,
                    _net_pnl,
                    session_realized_pnl,
                    _session_fees,
                    _session_net_pnl,
                ) = pnl_data
            
                if self.check_stop_conditions(realized_pnl, unrealized_pnl, session_realized_pnl):
                    self._stop_trading = True
                    logger.warning("触发风控条件，提前结束策略迭代")
                    break
            
                wait_time = interval_seconds
                logger.info(f"等待 {wait_time} 秒后进行下一次迭代...")
                time.sleep(wait_time)                 

            # 结束运行时打印最终报表
            logger.info("\n=== 做市策略运行结束 ===")
            if self._stop_trading and self.stop_reason:
                logger.warning(f"提前停止原因: {self.stop_reason}")
            self.print_trading_stats()
            
            # 打印本次执行的最终统计摘要
            logger.info("\n=== 本次执行统计摘要 ===")
            session_buy_volume = sum(qty for _, qty in self.session_buy_trades)
            session_sell_volume = sum(qty for _, qty in self.session_sell_trades)
            session_total_volume = session_buy_volume + session_sell_volume
            session_profit = self._calculate_session_profit()
            
            # 计算执行时间
            td = datetime.now() - self.session_start_time
            total_seconds = int(td.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            run_time = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            logger.info(f"执行时间: {run_time}")
            
            logger.info(f"总成交量: {session_total_volume} {self.base_asset}")
            logger.info(f"买入量: {session_buy_volume} {self.base_asset}, 卖出量: {session_sell_volume} {self.base_asset}")
            logger.info(f"Maker买入: {self.session_maker_buy_volume} {self.base_asset}, Maker卖出: {self.session_maker_sell_volume} {self.base_asset}")
            logger.info(f"Taker买入: {self.session_taker_buy_volume} {self.base_asset}, Taker卖出: {self.session_taker_sell_volume} {self.base_asset}")
            logger.info(f"已实现利润: {session_profit:.8f} {self.quote_asset}")
            logger.info(f"总手续费: {self.session_fees:.8f} {self.quote_asset}")
            logger.info(f"净利润: {(session_profit - self.session_fees):.8f} {self.quote_asset}")
            
            if session_total_volume > 0:
                logger.info(f"每单位成交量利润: {((session_profit - self.session_fees) / session_total_volume):.8f} {self.quote_asset}/{self.base_asset}")
        
        except KeyboardInterrupt:
            logger.info("\n用户中断，停止做市")
            
            # 中断时也打印本次执行的统计数据
            logger.info("\n=== 本次执行统计摘要(中断) ===")
            session_buy_volume = sum(qty for _, qty in self.session_buy_trades)
            session_sell_volume = sum(qty for _, qty in self.session_sell_trades)
            session_total_volume = session_buy_volume + session_sell_volume
            session_profit = self._calculate_session_profit()
            
            # 计算执行时间
            td = datetime.now() - self.session_start_time
            total_seconds = int(td.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60
            run_time = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            logger.info(f"执行时间: {run_time}")
            
            logger.info(f"总成交量: {session_total_volume} {self.base_asset}")
            logger.info(f"买入量: {session_buy_volume} {self.base_asset}, 卖出量: {session_sell_volume} {self.base_asset}")
            logger.info(f"Maker买入: {self.session_maker_buy_volume} {self.base_asset}, Maker卖出: {self.session_maker_sell_volume} {self.base_asset}")
            logger.info(f"Taker买入: {self.session_taker_buy_volume} {self.base_asset}, Taker卖出: {self.session_taker_sell_volume} {self.base_asset}")
            logger.info(f"已实现利润: {session_profit:.8f} {self.quote_asset}")
            logger.info(f"总手续费: {self.session_fees:.8f} {self.quote_asset}")
            logger.info(f"净利润: {(session_profit - self.session_fees):.8f} {self.quote_asset}")
            
            if session_total_volume > 0:
                logger.info(f"每单位成交量利润: {((session_profit - self.session_fees) / session_total_volume):.8f} {self.quote_asset}/{self.base_asset}")
        
        finally:
            logger.info("取消所有未成交订单...")
            self.cancel_existing_orders()
            
            # 关闭 WebSocket
            if self.ws:
                self.ws.close()
            
            # 关闭数据库连接
            if self.db:
                self.db.close()
                logger.info("数据库连接已关闭")
