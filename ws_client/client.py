"""
WebSocket客户端模块
"""
import json
import time
import threading
from collections import deque
from typing import Dict, Any, Optional, Callable
import websocket as ws
from config import WS_URL, DEFAULT_WINDOW
from api.auth import create_signature
from api.bp_client import BPClient
from utils.helpers import calculate_volatility
from logger import setup_logger
from urllib.parse import urlparse

logger = setup_logger("backpack_ws")

class BackpackWebSocket:
    def __init__(self, api_key, secret_key, symbol, on_message_callback=None, auto_reconnect=True, proxy=None):
        """
        初始化WebSocket客户端
        
        Args:
            api_key: API密钥
            secret_key: API密钥
            symbol: 交易对符号
            on_message_callback: 消息回调函数
            auto_reconnect: 是否自动重连
            proxy:  wss代理 支持格式为 http://user:pass@host:port/ 或者 http://host:port

        """
        self.api_key = api_key
        self.secret_key = secret_key
        self.symbol = symbol
        self.ws = None
        self.on_message_callback = on_message_callback
        self.connected = False
        self.last_price = None
        self.bid_price = None
        self.ask_price = None
        self.orderbook = {"bids": [], "asks": []}
        self.order_updates = []
        self.historical_prices = []  # 储存历史价格用于计算波动率
        self.max_price_history = 100  # 最多储存的价格数量
        
        # 重连相关参数
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = 1
        self.max_reconnect_delay = 1800
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 2
        self.reconnect_cooldown_until = 0.0
        self.running = False
        self.ws_thread = None
        
        # 记录已订阅的频道
        self.subscriptions = []
        
        # 添加WebSocket线程锁
        self.ws_lock = threading.Lock()
        
        # 添加心跳检测
        self.last_heartbeat = time.time()
        self.heartbeat_interval = 30
        self.heartbeat_thread = None

        # 添加代理参数
        self.proxy = proxy
        
        # 客户端缓存，避免重复创建实例
        self._client_cache = {}
        
        # 添加重连中标志，避免多次重连
        self.reconnecting = False

        # API 备援方案相关属性
        self.api_fallback_thread = None
        self.api_fallback_active = False
        self.api_poll_interval = 2  # 秒

        # REST 订单更新追踪
        self._fallback_bootstrapped = False
        self._seen_fill_ids = deque(maxlen=200)
        self._seen_fill_id_set = set()
        self._last_fill_timestamp = 0

    def _get_client(self):
        """获取缓存的客户端实例，避免重复创建"""
        cache_key = "public"
        if cache_key not in self._client_cache:
            self._client_cache[cache_key] = BPClient({
                "api_key": self.api_key,
                "secret_key": self.secret_key,
            })
        return self._client_cache[cache_key]
    
    def _start_api_fallback(self):
        """启动使用 REST API 的备援模式"""
        if self.api_fallback_active:
            return

        logger.warning("WebSocket 异常，启动 API 备援模式以持续获取数据")
        self.api_fallback_active = True
        self._fallback_bootstrapped = False
        self.api_fallback_thread = threading.Thread(target=self._api_fallback_loop, daemon=True)
        self.api_fallback_thread.start()

    def _stop_api_fallback(self):
        """停止 REST API 备援模式"""
        if not self.api_fallback_active:
            return

        self.api_fallback_active = False

        if self.api_fallback_thread and self.api_fallback_thread.is_alive():
            try:
                self.api_fallback_thread.join(timeout=1)
            except Exception:
                pass

        self.api_fallback_thread = None

    def _api_fallback_loop(self):
        """循环通过 REST API 更新行情信息"""
        client = self._get_client()

        while self.running and self.api_fallback_active:
            try:
                order_book = client.get_order_book(self.symbol, 50)
                ticker = client.get_ticker(self.symbol)
                fills = client.get_fill_history(self.symbol, limit=100)

                if isinstance(order_book, dict) and "error" not in order_book:
                    bids = order_book.get("bids", [])
                    asks = order_book.get("asks", [])

                    if bids or asks:
                        self.orderbook = {"bids": bids, "asks": asks}

                        if bids:
                            self.bid_price = bids[0][0]
                        if asks:
                            self.ask_price = asks[0][0]

                        if self.on_message_callback:
                            depth_event = {
                                "b": [[str(price), str(quantity)] for price, quantity in bids],
                                "a": [[str(price), str(quantity)] for price, quantity in asks],
                                "source": "api"
                            }
                            self.on_message_callback(f"depth.{self.symbol}", depth_event)

                if isinstance(ticker, dict) and "error" not in ticker:
                    bid_raw = ticker.get("bidPrice") or ticker.get("bestBidPrice")
                    ask_raw = ticker.get("askPrice") or ticker.get("bestAskPrice")
                    last_raw = ticker.get("lastPrice") or ticker.get("price")

                    def _safe_float(value):
                        try:
                            return float(value)
                        except (TypeError, ValueError):
                            return None

                    bid = _safe_float(bid_raw)
                    ask = _safe_float(ask_raw)
                    last = _safe_float(last_raw)

                    if bid is not None:
                        self.bid_price = bid
                    if ask is not None:
                        self.ask_price = ask
                    if last is not None:
                        self.last_price = last
                        self.add_price_to_history(self.last_price)

                    if self.on_message_callback:
                        ticker_event = {
                            "b": str(self.bid_price) if self.bid_price is not None else None,
                            "a": str(self.ask_price) if self.ask_price is not None else None,
                            "p": str(self.last_price) if self.last_price is not None else None,
                            "source": "api"
                        }
                        self.on_message_callback(f"bookTicker.{self.symbol}", ticker_event)

                # 若仍未获得价格信息，尝试以订单簿估算
                if self.last_price is None and self.bid_price and self.ask_price:
                    self.last_price = (self.bid_price + self.ask_price) / 2
                    self.add_price_to_history(self.last_price)

                # 通过 REST 补充订单成交通知
                normalised_fills = self._normalise_fill_history_response(fills)
                if normalised_fills:
                    self._process_rest_fill_updates(normalised_fills)

            except Exception as e:
                logger.error(f"API 备援获取数据时出错: {e}")

            # 控制轮询频率，避免触发限速
            time.sleep(self.api_poll_interval)

    def _normalise_fill_history_response(self, response):
        """解析 REST 返回的成交列表"""
        if isinstance(response, dict) and "error" in response:
            return []

        data = response
        if isinstance(response, dict):
            data = response.get("data", response)
            if isinstance(data, dict):
                for key in ("fills", "items", "rows", "records", "list"):
                    value = data.get(key)
                    if isinstance(value, list):
                        data = value
                        break
        if not isinstance(data, list):
            return []

        fills = []

        def _extract(entry: Dict[str, Any], *keys):
            for key in keys:
                if key in entry and entry[key] not in (None, ""):
                    return entry[key]
            return None

        for entry in data:
            if not isinstance(entry, dict):
                continue

            fill_id = _extract(entry, "id", "fillId", "fill_id", "tradeId", "trade_id", "executionId", "execution_id", "t")
            order_id = _extract(entry, "orderId", "order_id", "i")
            side = _extract(entry, "side", "S")
            price = _extract(entry, "price", "p", "L")
            quantity = _extract(entry, "size", "quantity", "qty", "q", "l")
            fee = _extract(entry, "fee", "commission", "n")
            fee_asset = _extract(entry, "feeAsset", "commissionAsset", "N")
            maker_flag = _extract(entry, "isMaker", "maker", "m")
            timestamp = _extract(entry, "timestamp", "time", "ts", "T")

            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None

            try:
                quantity = float(quantity) if quantity is not None else None
            except (TypeError, ValueError):
                quantity = None

            try:
                fee = float(fee) if fee is not None else 0.0
            except (TypeError, ValueError):
                fee = 0.0

            try:
                timestamp = int(timestamp)
            except (TypeError, ValueError):
                timestamp = 0

            if not fill_id and timestamp:
                fill_id = str(timestamp)

            fills.append({
                "fill_id": str(fill_id) if fill_id is not None else None,
                "order_id": str(order_id) if order_id is not None else None,
                "side": side,
                "price": price,
                "quantity": quantity,
                "fee": fee,
                "fee_asset": fee_asset,
                "is_maker": bool(maker_flag) if isinstance(maker_flag, bool) else str(maker_flag).lower() in ("true", "1", "yes"),
                "timestamp": timestamp,
            })

        return [fill for fill in fills if fill["order_id"] and fill["quantity"]]

    def _process_rest_fill_updates(self, fills):
        """处理 REST 备援获取到的成交信息"""
        fills = sorted(fills, key=lambda item: item.get("timestamp", 0))

        if not self._fallback_bootstrapped:
            for fill in fills:
                self._register_fill_seen(fill)
            self._fallback_bootstrapped = True
            return

        for fill in fills:
            if self._is_new_fill(fill):
                self._register_fill_seen(fill)
                self._emit_rest_order_fill(fill)

    def _is_new_fill(self, fill: Dict[str, Any]) -> bool:
        fill_id = fill.get("fill_id")
        timestamp = fill.get("timestamp", 0)

        if fill_id and fill_id in self._seen_fill_id_set:
            return False

        if timestamp and timestamp <= self._last_fill_timestamp and not fill_id:
            return False

        return True

    def _register_fill_seen(self, fill: Dict[str, Any]):
        fill_id = fill.get("fill_id")
        timestamp = fill.get("timestamp", 0)

        if fill_id:
            if len(self._seen_fill_ids) >= self._seen_fill_ids.maxlen:
                oldest = self._seen_fill_ids.popleft()
                if oldest in self._seen_fill_id_set:
                    self._seen_fill_id_set.remove(oldest)
            self._seen_fill_ids.append(fill_id)
            self._seen_fill_id_set.add(fill_id)

        if timestamp:
            self._last_fill_timestamp = max(self._last_fill_timestamp, timestamp)

    def _emit_rest_order_fill(self, fill: Dict[str, Any]):
        if not self.on_message_callback:
            return

        side = (fill.get("side") or "").lower()
        if side in ("buy", "bid"):
            ws_side = "Bid"
        elif side in ("sell", "ask"):
            ws_side = "Ask"
        else:
            ws_side = side.upper() if side else None

        if ws_side is None:
            return

        event = {
            "e": "orderFill",
            "S": ws_side,
            "l": str(fill.get("quantity", "0")),
            "L": str(fill.get("price", "0")),
            "i": fill.get("order_id"),
            "m": fill.get("is_maker", True),
            "n": str(fill.get("fee", 0.0)),
            "N": fill.get("fee_asset"),
            "t": fill.get("fill_id"),
            "source": "api-fallback",
        }

        logger.info(
            f"REST备援检测到成交: 订单 {event['i']} | 方向 {event['S']} | 数量 {event['l']} | 价格 {event['L']}"
        )

        self.on_message_callback(f"account.orderUpdate.{self.symbol}", event)

    def initialize_orderbook(self):
        """通过REST API获取订单簿初始快照"""
        try:
            # 使用REST API获取完整订单簿
            order_book = self._get_client().get_order_book(self.symbol, 100)  # 增加深度
            if isinstance(order_book, dict) and "error" in order_book:
                logger.error(f"订单簿初始化失败: {order_book['error']}")
                return False
            
            # 重置并填充orderbook数据结构
            bids = order_book.get("bids", [])
            asks = order_book.get("asks", [])
            self.orderbook = {"bids": bids, "asks": asks}
            
            logger.info(f"订单薄初始化成功: {len(self.orderbook['bids'])} 个买单, {len(self.orderbook['asks'])} 个卖单")
            
            # 初始化最高买价和最低卖价
            if self.orderbook["bids"]:
                self.bid_price = self.orderbook["bids"][0][0]
            if self.orderbook["asks"]:
                self.ask_price = self.orderbook["asks"][0][0]
            if self.bid_price and self.ask_price:
                self.last_price = (self.bid_price + self.ask_price) / 2
                self.add_price_to_history(self.last_price)
            
            return True
        except Exception as e:
            logger.error(f"初始化订单簿时出错: {e}")
            return False
    
    def add_price_to_history(self, price):
        """添加价格到历史记录用于计算波动率"""
        if price:
            self.historical_prices.append(price)
            # 保持历史记录在设定长度内
            if len(self.historical_prices) > self.max_price_history:
                self.historical_prices = self.historical_prices[-self.max_price_history:]
    
    def get_volatility(self, window=20):
        """获取当前波动率"""
        return calculate_volatility(self.historical_prices, window)
    
    def start_heartbeat(self):
        """开始心跳检测线程"""
        if self.heartbeat_thread is None or not self.heartbeat_thread.is_alive():
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_check, daemon=True)
            self.heartbeat_thread.start()
    
    def _heartbeat_check(self):
        """定期检查WebSocket连接状态并在需要时重连"""
        while self.running:
            current_time = time.time()
            
            # 检查是否在冷却期，如果是则跳过心跳检测
            if self.reconnect_cooldown_until and current_time < self.reconnect_cooldown_until:
                remaining_cooldown = int(self.reconnect_cooldown_until - current_time)
                logger.debug(f"WebSocket 处于冷却期，剩余 {remaining_cooldown} 秒，使用 API 备援模式")
                time.sleep(5)
                continue
            
            time_since_last_heartbeat = current_time - self.last_heartbeat
            
            if time_since_last_heartbeat > self.heartbeat_interval * 2:
                logger.warning(f"心跳检测超时 ({time_since_last_heartbeat:.1f}秒)，尝试重新连接")
                # 使用非阻塞方式触发重连
                threading.Thread(target=self._trigger_reconnect, daemon=True).start()
                
            time.sleep(5)  # 每5秒检查一次
    
    def _trigger_reconnect(self):
        """非阻塞触发重连"""
        current_time = time.time()
        if self.reconnect_cooldown_until and current_time < self.reconnect_cooldown_until:
            logger.debug("重连尚在冷却期，跳过此次请求")
            return

        if self.reconnect_cooldown_until and current_time >= self.reconnect_cooldown_until:
            self.reconnect_attempts = 0
            self.reconnect_cooldown_until = 0.0

        if not self.reconnecting:
            self.reconnect()
        
    def connect(self):
        """建立WebSocket连接"""
        try:
            self.running = True
            self.reconnect_attempts = 0
            self.reconnect_cooldown_until = 0.0
            self.reconnecting = False
            ws.enableTrace(False)
            self.ws = ws.WebSocketApp(
                WS_URL,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_ping=self.on_ping,
                on_pong=self.on_pong
            )
            
            self.ws_thread = threading.Thread(target=self.ws_run_forever)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
            # 启动心跳检测
            self.start_heartbeat()
        except Exception as e:
            logger.error(f"初始化WebSocket连接时出错: {e}")
            self._start_api_fallback()
    
    def ws_run_forever(self):
        """WebSocket运行循环 - 修复版本"""
        try:
            if hasattr(self.ws, 'sock') and self.ws.sock and self.ws.sock.connected:
                logger.debug("发现socket已经打开，跳过run_forever")
                return

            http_proxy_host = None
            http_proxy_port = None
            http_proxy_auth = None
            proxy_type = None

            if self.proxy:
                # 使用标准库 urlparse 进行可靠的解析
                parsed_proxy = urlparse(self.proxy)
                
                # 建立安全的日志信息，隐藏密码
                safe_proxy_display = f"{parsed_proxy.scheme}://{parsed_proxy.hostname}:{parsed_proxy.port}"
                if parsed_proxy.username:
                    safe_proxy_display = f"{parsed_proxy.scheme}://{parsed_proxy.username}:***@{parsed_proxy.hostname}:{parsed_proxy.port}"

                logger.info(f"正在使用 WebSocket 代理: {safe_proxy_display}")

                http_proxy_host = parsed_proxy.hostname
                http_proxy_port = parsed_proxy.port
                if parsed_proxy.username and parsed_proxy.password:
                    http_proxy_auth = (parsed_proxy.username, parsed_proxy.password)
                # 支持 http, socks4, socks5 代理
                proxy_type = parsed_proxy.scheme if parsed_proxy.scheme in ['http', 'socks4', 'socks5'] else 'http'
                
            # 将解析后的参数传递给 run_forever
            self.ws.run_forever(
                ping_interval=self.heartbeat_interval,
                ping_timeout=10,
                http_proxy_host=http_proxy_host,
                http_proxy_port=http_proxy_port,
                http_proxy_auth=http_proxy_auth,
                proxy_type=proxy_type
            )

        except Exception as e:
            logger.error(f"WebSocket运行时错误: {e}")
        finally:
            logger.debug("WebSocket run_forever 执行结束")
    
    def on_pong(self, ws, message):
        """处理pong响应"""
        self.last_heartbeat = time.time()
        
    def reconnect(self):
        """完全断开并重新建立WebSocket连接 - 修复版本"""
        # 防止多次重连
        if self.reconnecting:
            logger.debug("重连已在进行中，跳过此次重连请求")
            return False

        current_time = time.time()
        if self.reconnect_cooldown_until and current_time < self.reconnect_cooldown_until:
            logger.debug("重连尚未解除冷却，跳过此次重连请求")
            return False

        with self.ws_lock:
            if not self.running or self.reconnect_attempts >= self.max_reconnect_attempts:
                logger.warning(f"重连次数上限 ({self.max_reconnect_attempts})，暂停自动重连")
                cooldown_seconds = max(self.max_reconnect_delay, 60)
                self.reconnect_cooldown_until = time.time() + cooldown_seconds
                self.last_heartbeat = time.time()
                logger.warning(f"已启动 {cooldown_seconds} 秒冷却，将继续使用后备模式")
                self._start_api_fallback()
                return False

            self.reconnecting = True
            self.reconnect_attempts += 1
            delay = min(self.reconnect_delay * (2 ** (self.reconnect_attempts - 1)), self.max_reconnect_delay)
            
            logger.info(f"尝试第 {self.reconnect_attempts} 次连接，等待 {delay} 秒...")
            time.sleep(delay)
            
            # 确保完全断开连接前先标记连接状态
            self.connected = False
            
            # 优雅关闭现有连接
            self._force_close_connection()
            
            # 重置所有相关状态
            self.ws_thread = None
            self.subscriptions = []  # 清空订阅列表，以便重新订阅
            
            try:
                # 创建全新的WebSocket连接
                ws.enableTrace(False)
                self.ws = ws.WebSocketApp(
                    WS_URL,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                    on_ping=self.on_ping,
                    on_pong=self.on_pong
                )
                
                # 创建新线程
                self.ws_thread = threading.Thread(target=self.ws_run_forever)
                self.ws_thread.daemon = True
                self.ws_thread.start()

                # 更新最后心跳时间，避免重连后立即触发心跳检测
                self.last_heartbeat = time.time()
                self.reconnect_cooldown_until = 0.0

                logger.info(f"第 {self.reconnect_attempts} 次重连已经启动")
                
                self.reconnecting = False
                return True
                
            except Exception as e:
                logger.error(f"重连途中出错: {e}")
                self.reconnecting = False
                self._start_api_fallback()
                return False
    
    def _force_close_connection(self):
        """强制关闭现有连接"""
        try:
            # 完全断开并清理之前的WebSocket连接
            if self.ws:
                try:
                    # 显式设置内部标记表明这是用户主动关闭
                    if hasattr(self.ws, '_closed_by_me'):
                        self.ws._closed_by_me = True
                    
                    # 关闭WebSocket
                    self.ws.close()
                    if hasattr(self.ws, 'keep_running'):
                        self.ws.keep_running = False
                    
                    # 强制关闭socket
                    if hasattr(self.ws, 'sock') and self.ws.sock:
                        try:
                            self.ws.sock.close()
                            self.ws.sock = None
                        except:
                            pass
                except Exception as e:
                    logger.debug(f"关闭ws的预期错误: {e}")
                
                self.ws = None
                
            # 处理旧线程 - 使用较短的超时时间
            if self.ws_thread and self.ws_thread.is_alive():
                try:
                    # 不要无限等待线程结束
                    self.ws_thread.join(timeout=1.0)
                    if self.ws_thread.is_alive():
                        logger.warning("旧线程未能在超时时间内结束，但继续重连过程")
                except Exception as e:
                    logger.debug(f"等待线程中止失败: {e}")
            
            # 给系统少量时间清理资源
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"强制关闭连接出错: {e}")
        
    def on_ping(self, ws, message):
        """处理ping消息"""
        try:
            self.last_heartbeat = time.time()
            if ws and hasattr(ws, 'sock') and ws.sock:
                ws.sock.pong(message)
            else:
                logger.debug("无法ping：WebSocket或sock为None")
        except Exception as e:
            logger.debug(f"ping失败: {e}")
        
    def on_open(self, ws):
        """WebSocket打开时的处理"""
        logger.info("WebSocket链接已建立")
        self.connected = True
        self.reconnect_attempts = 0
        self.reconnecting = False
        self.last_heartbeat = time.time()
        
        # 停止 API 备援模式
        self._stop_api_fallback()

        # 添加短暂延迟确保连接稳定
        time.sleep(0.5)
        
        # 初始化订单簿
        orderbook_initialized = self.initialize_orderbook()
        
        # 如果初始化成功，订阅深度和行情数据
        if orderbook_initialized:
            if "bookTicker" in self.subscriptions or not self.subscriptions:
                self.subscribe_bookTicker()
            
            if "depth" in self.subscriptions or not self.subscriptions:
                self.subscribe_depth()
        
        # 重新订阅私有订单更新流
        for sub in self.subscriptions:
            if sub.startswith("account."):
                self.private_subscribe(sub)
    
    def subscribe_bookTicker(self):
        """订阅最优价格"""
        logger.info(f"订阅 {self.symbol} 的bookTicker...")
        if not self.connected or not self.ws:
            logger.warning("WebSocket未连接，无法订阅bookTicker")
            return False
            
        try:
            message = {
                "method": "SUBSCRIBE",
                "params": [f"bookTicker.{self.symbol}"]
            }
            self.ws.send(json.dumps(message))
            if "bookTicker" not in self.subscriptions:
                self.subscriptions.append("bookTicker")
            return True
        except Exception as e:
            logger.error(f"订阅bookTicker失败: {e}")
            return False
    
    def subscribe_depth(self):
        """订阅深度信息"""
        logger.info(f"订阅 {self.symbol} 的深度信息...")
        if not self.connected or not self.ws:
            logger.warning("WebSocket未连接，无法订阅市场深度信息。")
            return False
            
        try:
            message = {
                "method": "SUBSCRIBE",
                "params": [f"depth.{self.symbol}"]
            }
            self.ws.send(json.dumps(message))
            if "depth" not in self.subscriptions:
                self.subscriptions.append("depth")
            return True
        except Exception as e:
            logger.error(f"订阅深度信息失败: {e}")
            return False
    
    def private_subscribe(self, stream):
        """订阅私有数据流"""
        if not self.connected or not self.ws:
            logger.warning("WebSocket未连接，无法订阅私有流")
            return False
            
        try:
            timestamp = str(int(time.time() * 1000))
            window = DEFAULT_WINDOW
            sign_message = f"instruction=subscribe&timestamp={timestamp}&window={window}"
            signature = create_signature(self.secret_key, sign_message)
            
            if not signature:
                logger.error("签名失败，无法订阅ws私有流")
                return False
            
            message = {
                "method": "SUBSCRIBE",
                "params": [stream],
                "signature": [self.api_key, signature, timestamp, window]
            }
            
            self.ws.send(json.dumps(message))
            logger.info(f"已订阅私有流: {stream}")
            if stream not in self.subscriptions:
                self.subscriptions.append(stream)
            return True
        except Exception as e:
            logger.error(f"订阅私有流失败: {e}")
            return False
    
    def on_message(self, ws, message):
        """处理WebSocket消息"""
        try:
            data = json.loads(message)
            
            # 处理ping pong消息
            if isinstance(data, dict) and data.get("ping"):
                pong_message = {"pong": data.get("ping")}
                if self.ws and self.connected:
                    self.ws.send(json.dumps(pong_message))
                    self.last_heartbeat = time.time()
                return
            
            if "stream" in data and "data" in data:
                stream = data["stream"]
                event_data = data["data"]
                
                # 处理bookTicker
                if stream.startswith("bookTicker."):
                    if 'b' in event_data and 'a' in event_data:
                        self.bid_price = float(event_data['b'])
                        self.ask_price = float(event_data['a'])
                        self.last_price = (self.bid_price + self.ask_price) / 2
                        # 记录历史价格用于计算波动率
                        self.add_price_to_history(self.last_price)
                
                # 处理depth
                elif stream.startswith("depth."):
                    if 'b' in event_data and 'a' in event_data:
                        self._update_orderbook(event_data)
                
                # 订单更新数据流
                elif stream.startswith("account.orderUpdate."):
                    self.order_updates.append(event_data)
                    
                if self.on_message_callback:
                    self.on_message_callback(stream, event_data)
            
        except Exception as e:
            logger.error(f"处理ws消息出错: {e}")
    
    def _update_orderbook(self, data):
        """更新订单簿（优化处理速度）"""
        # 处理买单更新
        if 'b' in data:
            for bid in data['b']:
                price = float(bid[0])
                quantity = float(bid[1])
                
                # 使用二分查找来优化插入位置查找
                if quantity == 0:
                    # 移除价位
                    self.orderbook["bids"] = [b for b in self.orderbook["bids"] if b[0] != price]
                else:
                    # 先查找是否存在相同价位
                    found = False
                    for i, b in enumerate(self.orderbook["bids"]):
                        if b[0] == price:
                            self.orderbook["bids"][i] = [price, quantity]
                            found = True
                            break
                    
                    # 如果不存在，插入并保持排序
                    if not found:
                        self.orderbook["bids"].append([price, quantity])
                        # 按价格降序排序
                        self.orderbook["bids"] = sorted(self.orderbook["bids"], key=lambda x: x[0], reverse=True)
        
        # 处理卖单更新
        if 'a' in data:
            for ask in data['a']:
                price = float(ask[0])
                quantity = float(ask[1])
                
                if quantity == 0:
                    # 移除价位
                    self.orderbook["asks"] = [a for a in self.orderbook["asks"] if a[0] != price]
                else:
                    # 先查找是否存在相同价位
                    found = False
                    for i, a in enumerate(self.orderbook["asks"]):
                        if a[0] == price:
                            self.orderbook["asks"][i] = [price, quantity]
                            found = True
                            break
                    
                    # 如果不存在，插入并保持排序
                    if not found:
                        self.orderbook["asks"].append([price, quantity])
                        # 按价格升序排序
                        self.orderbook["asks"] = sorted(self.orderbook["asks"], key=lambda x: x[0])
    
    def on_error(self, ws, error):
        """处理WebSocket错误"""
        logger.error(f"WebSocket发生错误: {error}")
        self.last_heartbeat = 0  # 强制触发重连

        self._start_api_fallback()
    
    def on_close(self, ws, close_status_code, close_msg):
        """处理WebSocket关闭"""
        previous_connected = self.connected
        self.connected = False
        logger.info(f"WebSocket链接已经关闭: {close_msg if close_msg else 'No message'} (状态码: {close_status_code if close_status_code else 'None'})")
        
        # 清理当前socket资源
        if hasattr(ws, 'sock') and ws.sock:
            try:
                ws.sock.close()
                ws.sock = None
            except Exception as e:
                logger.debug(f"关闭ws时出错: {e}")
        
        if close_status_code == 1000 or getattr(ws, '_closed_by_me', False):
            logger.info("WebSocket正常关闭")
            self._start_api_fallback()
        elif previous_connected and self.running and self.auto_reconnect and not self.reconnecting:
            logger.info("WebSocket非正常关闭，重连中")
            # 使用线程触发重连，避免在回调中直接重连
            threading.Thread(target=self._trigger_reconnect, daemon=True).start()
            self._start_api_fallback()
    
    def close(self):
        """完全关闭WebSocket连接"""
        logger.info("主动关闭WebSocket连接...")
        self.running = False
        self.connected = False
        self.reconnecting = False
        self.reconnect_cooldown_until = 0.0
        
        # 停止心跳检测线程
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            try:
                self.heartbeat_thread.join(timeout=1)
            except Exception:
                pass
        self.heartbeat_thread = None
        
        # 强制关闭连接
        self._force_close_connection()
        
        # 重置订阅状态
        self.subscriptions = []

        # 确保停止 API 备援
        self._stop_api_fallback()
        
        logger.info("WebSocket连接已完全关闭")
    
    def get_current_price(self):
        """获取当前价格"""
        return self.last_price
    
    def get_bid_ask(self):
        """获取买卖价"""
        return self.bid_price, self.ask_price
    
    def get_orderbook(self):
        """获取订单簿"""
        return self.orderbook

    def is_connected(self):
        """检查连接状态"""
        if not self.connected:
            return False
        if not self.ws:
            return False
        if not hasattr(self.ws, 'sock') or not self.ws.sock:
            return False
        
        # 检查socket是否连接
        try:
            return self.ws.sock.connected
        except:
            return False
    
    def get_liquidity_profile(self, depth_percentage=0.01):
        """分析市场流动性特征"""
        if not self.orderbook["bids"] or not self.orderbook["asks"]:
            return None
        
        mid_price = (self.bid_price + self.ask_price) / 2 if self.bid_price and self.ask_price else None
        if not mid_price:
            return None
        
        # 计算价格范围
        min_price = mid_price * (1 - depth_percentage)
        max_price = mid_price * (1 + depth_percentage)
        
        # 分析买卖单流动性
        bid_volume = sum(qty for price, qty in self.orderbook["bids"] if price >= min_price)
        ask_volume = sum(qty for price, qty in self.orderbook["asks"] if price <= max_price)
        
        # 计算买卖比例
        ratio = bid_volume / ask_volume if ask_volume > 0 else float('inf')
        
        # 买卖压力差异
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume) if (bid_volume + ask_volume) > 0 else 0
        
        return {
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'volume_ratio': ratio,
            'imbalance': imbalance,
            'mid_price': mid_price
        }
    
    def check_and_reconnect_if_needed(self):
        """检查连接状态并在需要时重连 - 供外部调用"""
        current_time = time.time()
        
        # 如果在冷却期内，确保 API 备援模式激活，但不触发重连
        if self.reconnect_cooldown_until and current_time < self.reconnect_cooldown_until:
            if not self.is_connected() and not self.api_fallback_active:
                remaining_cooldown = int(self.reconnect_cooldown_until - current_time)
                logger.debug(f"冷却器检测到ws断开（剩余 {remaining_cooldown} 秒），启动api后备模式")
                self._start_api_fallback()
            return self.is_connected()

        # 冷却期已结束，重置重连计数器
        if self.reconnect_cooldown_until and current_time >= self.reconnect_cooldown_until:
            self.reconnect_attempts = 0
            self.reconnect_cooldown_until = 0.0
            logger.info("冷却期结束，重置尝试次数")

        # 非冷却期，检查连接并触发重连
        if not self.is_connected() and not self.reconnecting:
            logger.info("检测到ws链接断开，重试...")
            threading.Thread(target=self._trigger_reconnect, daemon=True).start()
            self._start_api_fallback()

        return self.is_connected()
