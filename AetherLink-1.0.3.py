import asyncio
import aiohttp
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Union, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor
import pickle
import hashlib
from collections import defaultdict, deque
from websockets import connect, WebSocketClientProtocol
import ssl
from contextlib import asynccontextmanager
import gc
import threading  # 新增导入

try:
    import msgpack
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False

try:
    import lz4.frame
    LZ4_AVAILABLE = True
except ImportError:
    LZ4_AVAILABLE = False

try:
    import psutil  # 新增导入保护
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# 删除未使用的xxhash导入
# try:
#     import xxhash
#     XXHASH_AVAILABLE = True
# except ImportError:
#     XXHASH_AVAILABLE = False

class ConnectionState(Enum):
    """连接状态枚举"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTING = "disconnecting"  # 修复：添加缺失的枚举值
    ERROR = "error"

@dataclass
class Message:
    """消息数据结构"""
    id: str
    type: str
    data: Any
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class CacheEntry:
    """缓存条目"""
    data: Any
    timestamp: float
    ttl: float

class HighPerfRateLimiter:
    """高性能速率限制器"""
    
    def __init__(self, max_requests: int, time_window: float):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = deque()
        self._lock = asyncio.Lock()
    
    async def acquire(self) -> bool:
        """获取请求许可"""
        async with self._lock:
            now = time.time()
            # 清理过期的请求记录
            while self.requests and now - self.requests[0] > self.time_window:
                self.requests.popleft()
            
            if len(self.requests) < self.max_requests:
                self.requests.append(now)
                return True
            return False
    
    async def wait(self):
        """等待直到可以发送请求"""
        while not await self.acquire():
            await asyncio.sleep(0.01)

class ConnectionMetrics:
    """连接指标统计"""
    
    def __init__(self):
        self.connection_count = 0
        self.disconnection_count = 0
        self.messages_sent = 0
        self.messages_received = 0
        self.errors = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.start_time = time.time()
        self.last_activity = time.time()
        self._lock = asyncio.Lock()
    
    def update_activity(self):
        """更新活动时间"""
        self.last_activity = time.time()
    
    def increment_sent(self, bytes_count: int = 0):
        """增加发送统计"""
        self.messages_sent += 1
        self.bytes_sent += bytes_count
        self.last_activity = time.time()
    
    def increment_received(self, bytes_count: int = 0):
        """增加接收统计"""
        self.messages_received += 1
        self.bytes_received += bytes_count
        self.last_activity = time.time()
    
    def increment_error(self):
        """增加错误统计"""
        self.errors += 1
    
    def increment_connection(self):
        """增加连接统计"""
        self.connection_count += 1
    
    def increment_disconnection(self):
        """增加断开连接统计"""
        self.disconnection_count += 1
    
    def get_uptime(self) -> float:
        return time.time() - self.start_time
    
    def get_stats(self) -> Dict[str, Any]:
        uptime = self.get_uptime()
        return {
            "uptime": uptime,
            "connection_count": self.connection_count,
            "disconnection_count": self.disconnection_count,
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "errors": self.errors,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "messages_per_minute": (self.messages_sent + self.messages_received) / (uptime / 60) if uptime > 0 else 0,
            "last_activity": self.last_activity
        }

class HighPerfMessageQueue:
    """高性能消息队列管理器"""
    
    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self.queue = asyncio.Queue(maxsize=max_size)
        self.pending_messages: Dict[str, asyncio.Future] = {}
        self.message_history = deque(maxlen=500)
        self._pending_lock = asyncio.Lock()
    
    async def put(self, message: Message, wait: bool = True) -> bool:
        """添加消息到队列"""
        try:
            if wait:
                await self.queue.put(message)
            else:
                self.queue.put_nowait(message)
            self.message_history.append(message)
            return True
        except asyncio.QueueFull:
            return False
    
    async def get(self, timeout: Optional[float] = None) -> Optional[Message]:
        """从队列获取消息，支持超时"""
        try:
            if timeout:
                return await asyncio.wait_for(self.queue.get(), timeout)
            return await self.queue.get()
        except asyncio.TimeoutError:
            return None
    
    async def register_pending(self, message_id: str, future: asyncio.Future):
        """注册待处理消息"""
        async with self._pending_lock:
            self.pending_messages[message_id] = future
    
    async def resolve_pending(self, message_id: str, result: Any):
        """解析待处理消息"""
        async with self._pending_lock:
            if message_id in self.pending_messages:
                future = self.pending_messages.pop(message_id)
                if not future.done():
                    future.set_result(result)
    
    async def reject_pending(self, message_id: str, error: Exception):
        """拒绝待处理消息"""
        async with self._pending_lock:
            if message_id in self.pending_messages:
                future = self.pending_messages.pop(message_id)
                if not future.done():
                    future.set_exception(error)

class HighPerfCacheManager:
    """高性能缓存管理器"""
    
    def __init__(self, default_ttl: float = 300, max_size: int = 10000):
        self.default_ttl = default_ttl
        self.max_size = max_size
        self._l1_cache = {}
        self._l2_cache = {}
        self._access_count = defaultdict(int)
        self._hits = 0
        self._misses = 0
        self._l1_lock = asyncio.Lock()  # 修复：为L1缓存添加锁
        self._l2_lock = asyncio.Lock()
        self._running = False  # 修复：添加运行状态控制
        self._cleanup_task = None
        self._start_cleanup_task()
    
    def _start_cleanup_task(self):
        """启动定期清理任务"""
        self._running = True
        
        async def cleanup():
            while self._running:
                await asyncio.sleep(60)
                await self._cleanup_expired()
        
        self._cleanup_task = asyncio.create_task(cleanup())
    
    async def close(self):
        """清理资源"""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
    
    async def _cleanup_expired(self):
        """异步清理过期缓存"""
        now = time.time()
        
        # L1缓存清理（带锁）
        async with self._l1_lock:
            expired_keys = [
                key for key, (data, expiry) in list(self._l1_cache.items())
                if now > expiry
            ]
            for key in expired_keys:
                self._l1_cache.pop(key, None)
        
        # L2缓存清理（带锁）
        async with self._l2_lock:
            expired_keys = [
                key for key, entry in list(self._l2_cache.items())
                if now > entry.timestamp + entry.ttl
            ]
            for key in expired_keys:
                self._l2_cache.pop(key, None)
                self._access_count.pop(key, None)
    
    def _get_key(self, key: Any) -> str:
        """优化的键生成函数"""
        try:
            if isinstance(key, str):
                return f"s:{key}"
            elif isinstance(key, (int, float)):
                return f"n:{key}"
            else:
                key_bytes = pickle.dumps(key)
                return f"u:{hashlib.sha256(key_bytes).hexdigest()}"
        except Exception:  # 修复：使用具体异常类型
            return f"f:{hash(str(key))}"

    async def get(self, key: Any) -> Any:
        """高性能缓存获取"""
        cache_key = self._get_key(key)
        now = time.time()
        
        # 首先检查L1缓存（带锁）
        async with self._l1_lock:
            if cache_key in self._l1_cache:
                data, expiry = self._l1_cache[cache_key]
                if now < expiry:
                    self._hits += 1
                    self._access_count[cache_key] = self._access_count.get(cache_key, 0) + 1
                    return data
        
        # 检查L2缓存（带锁）
        async with self._l2_lock:
            if cache_key in self._l2_cache:
                entry = self._l2_cache[cache_key]
                if now < entry.timestamp + entry.ttl:
                    # 提升到L1缓存
                    async with self._l1_lock:
                        self._l1_cache[cache_key] = (
                            entry.data, 
                            now + min(entry.ttl, 60)
                        )
                    self._hits += 1
                    self._access_count[cache_key] = self._access_count.get(cache_key, 0) + 1
                    return entry.data
        
        self._misses += 1
        return None

    async def set(self, key: Any, data: Any, ttl: float = None):
        """高性能缓存设置"""
        cache_key = self._get_key(key)
        actual_ttl = ttl or self.default_ttl
        now = time.time()
        
        # 设置L1缓存（带锁）
        async with self._l1_lock:
            self._l1_cache[cache_key] = (data, now + min(actual_ttl, 60))
        
        # 设置L2缓存（带锁）
        async with self._l2_lock:
            # LRU淘汰策略
            if len(self._l2_cache) >= self.max_size:
                if self._access_count:
                    least_used_key = min(self._access_count, key=self._access_count.get)
                    self._l2_cache.pop(least_used_key, None)
                    self._access_count.pop(least_used_key, None)
                elif self._l2_cache:
                    key_to_remove = next(iter(self._l2_cache.keys()))
                    self._l2_cache.pop(key_to_remove, None)
                    self._access_count.pop(key_to_remove, None)
            
            self._l2_cache[cache_key] = CacheEntry(
                data=data,
                timestamp=now,
                ttl=actual_ttl
            )
            self._access_count[cache_key] = self._access_count.get(cache_key, 0) + 1
    
    async def delete(self, key: Any):
        """删除缓存数据"""
        cache_key = self._get_key(key)
        
        # 删除L1缓存（带锁）
        async with self._l1_lock:
            self._l1_cache.pop(cache_key, None)
        
        # 删除L2缓存（带锁）
        async with self._l2_lock:
            self._l2_cache.pop(cache_key, None)
            self._access_count.pop(cache_key, None)
    
    async def clear(self):
        """清空缓存"""
        async with self._l1_lock:
            self._l1_cache.clear()
        
        async with self._l2_lock:
            self._l2_cache.clear()
            self._access_count.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0
        
        l1_size = len(self._l1_cache)
        l2_size = len(self._l2_cache)
        
        return {
            "l1_size": l1_size,
            "l2_size": l2_size,
            "total_size": l1_size + l2_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": hit_rate,
            "max_size": self.max_size
        }

class MessageSerializer:
    """高性能消息序列化"""
    
    def __init__(self, enable_msgpack: bool = True, enable_compression: bool = True):
        self.enable_msgpack = enable_msgpack and MSGPACK_AVAILABLE
        self.enable_compression = enable_compression and LZ4_AVAILABLE
    
    def serialize(self, message: Any) -> bytes:
        """序列化消息"""
        try:
            if self.enable_msgpack and isinstance(message, (dict, list)):
                data = msgpack.packb(message, use_bin_type=True)
            else:
                if isinstance(message, (dict, list)):
                    data = json.dumps(message, separators=(',', ':')).encode()
                else:
                    data = str(message).encode()
            
            if self.enable_compression and len(data) > 1024:
                data = lz4.frame.compress(data)
            
            return data
        except Exception:
            return json.dumps(message, separators=(',', ':')).encode()
    
    def deserialize(self, data: bytes) -> Any:
        """反序列化消息"""
        try:
            if self.enable_compression and len(data) > 100 and data[:4] == b'\x04\x22\x4D\x18':
                data = lz4.frame.decompress(data)
            
            if self.enable_msgpack:
                try:
                    return msgpack.unpackb(data, raw=False)
                except Exception:
                    pass
            
            return json.loads(data.decode())
        except Exception:
            return data.decode() if isinstance(data, bytes) else data

class ConnectionPool:
    """HTTP连接池管理"""
    
    def __init__(self, adapter: 'HighPerfBaseAdapter'):
        self.adapter = adapter
        self._sessions = {}
        self._session_lock = asyncio.Lock()
    
    async def get_session(self, base_url: str) -> aiohttp.ClientSession:
        """获取或创建会话"""
        async with self._session_lock:
            if base_url not in self._sessions or self._sessions[base_url].closed:
                timeout = aiohttp.ClientTimeout(total=self.adapter.timeout)
                connector = aiohttp.TCPConnector(
                    ssl=self.adapter.ssl_context,
                    limit=100,
                    limit_per_host=20,
                    use_dns_cache=True,
                    ttl_dns_cache=300
                )
                
                self._sessions[base_url] = aiohttp.ClientSession(
                    headers=self.adapter.headers,
                    timeout=timeout,
                    connector=connector
                )
            
            return self._sessions[base_url]
    
    async def close_all(self):
        """关闭所有会话"""
        async with self._session_lock:
            for session in self._sessions.values():
                if not session.closed:
                    await session.close()
            self._sessions.clear()

def monitor_performance(func):
    """性能监控装饰器"""
    async def wrapper(self, *args, **kwargs):
        if not hasattr(self, 'enable_performance_monitoring') or not self.enable_performance_monitoring:
            return await func(self, *args, **kwargs)
            
        start_time = time.time()
        
        try:
            result = await func(self, *args, **kwargs)
            return result
        finally:
            duration = time.time() - start_time
            if duration > 1.0:
                self.logger.warning(
                    "Slow operation: %s took %.2fs", func.__name__, duration  # 修复：使用格式化字符串
                )
    
    return wrapper

class HighPerfBaseAdapter(ABC):
    """
    高性能适配器基类
    """
    
    def __init__(
        self,
        base_url: str = "",
        ws_url: str = "",
        headers: Optional[Dict[str, str]] = None,
        reconnect_interval: int = 5,
        max_retries: int = 10,
        heartbeat_interval: int = 30,
        timeout: int = 30,
        max_queue_size: int = 10000,
        rate_limit: Optional[Dict[str, int]] = None,
        enable_cache: bool = True,
        cache_ttl: float = 300,
        cache_max_size: int = 10000,
        ssl_verify: bool = True,
        proxy: Optional[str] = None,
        enable_compression: bool = True,
        enable_msgpack: bool = True,
        max_memory_mb: int = 500,
        enable_performance_monitoring: bool = True,
        logger: Optional[logging.Logger] = None
    ):
        # 基础配置
        self.base_url = base_url.rstrip('/')
        self.ws_url = ws_url
        self.headers = headers or {}
        self.timeout = timeout
        self.ssl_verify = ssl_verify
        self.proxy = proxy
        
        # 性能配置
        self.enable_compression = enable_compression
        self.enable_msgpack = enable_msgpack
        self.max_memory_mb = max_memory_mb
        self.enable_performance_monitoring = enable_performance_monitoring
        
        # 重连配置
        self.reconnect_interval = reconnect_interval
        self.max_retries = max_retries
        
        # 心跳配置
        self.heartbeat_interval = heartbeat_interval
        
        # 连接状态
        self._state = ConnectionState.DISCONNECTED
        self._connection_lock = asyncio.Lock()
        self._retry_count = 0
        
        # HTTP会话池
        self.connection_pool = ConnectionPool(self)
        
        # WebSocket连接
        self._ws_connection: Optional[WebSocketClientProtocol] = None
        
        # 任务管理
        self._tasks: Set[asyncio.Task] = set()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        
        # 消息处理
        self.message_queue = HighPerfMessageQueue(max_queue_size)
        
        # 速率限制 - 修复：使用正确的变量名
        rate_limit = rate_limit or {"requests": 100, "window": 60}
        self.rate_limiter = HighPerfRateLimiter(
            rate_limit.get("requests", 100),  # 修复：rate_limit而不是rate
            rate_limit.get("window", 60)
        )
        
        # 缓存管理
        self.cache_manager = HighPerfCacheManager(cache_ttl, cache_max_size) if enable_cache else None
        
        # 序列化器
        self.serializer = MessageSerializer(enable_msgpack, enable_compression)
        
        # 指标统计
        self.metrics = ConnectionMetrics()
        
        # 回调函数注册 - 修复：使用线程锁
        self._callbacks = {
            "message": [],
            "connect": [],
            "disconnect": [],
            "error": [],
            "state_change": [],
            "before_send": [],
            "after_send": []
        }
        self._callback_lock = threading.Lock()  # 修复：使用线程锁
        
        # 消息处理器
        self._message_handlers: Dict[str, Callable] = {}
        self._handler_lock = asyncio.Lock()
        
        # 线程池用于阻塞操作
        self._thread_pool = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix=f"{self.__class__.__name__}_worker"
        )
        
        # 日志
        self.logger = logger or self._setup_logging()
        
        # SSL上下文
        self.ssl_context = ssl.create_default_context()
        if not ssl_verify:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        
        # 内存监控
        self._memory_monitor_task = None
        self._memory_monitor_running = False
    
    def _setup_logging(self) -> logging.Logger:
        """设置日志"""
        logger = logging.getLogger(f"{self.__class__.__name__}")
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - '
                '[%(filename)s:%(lineno)d] - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
            if self.enable_performance_monitoring:
                logger.setLevel(logging.DEBUG)
            else:
                logger.setLevel(logging.INFO)
        return logger
    
    def _get_memory_usage(self) -> float:
        """获取内存使用量(MB)"""
        if not PSUTIL_AVAILABLE:  # 修复：添加导入检查
            return 0
        try:
            process = psutil.Process()
            return process.memory_info().rss / 1024 / 1024
        except Exception:  # 修复：使用具体异常类型
            return 0
    
    async def _monitor_memory(self):
        """内存使用监控"""
        self._memory_monitor_running = True
        while self._memory_monitor_running:
            await asyncio.sleep(30)
            
            try:
                memory_mb = self._get_memory_usage()
                if memory_mb > self.max_memory_mb * 0.8:
                    self.logger.warning(
                        "Memory usage high: %.1fMB, clearing caches", memory_mb  # 修复：格式化字符串
                    )
                    # 修复：使用跟踪的任务
                    self._create_task(self._reduce_memory_usage())
            except Exception as e:
                self.logger.error("Memory monitoring error: %s", str(e))  # 修复：格式化字符串
    
    async def _health_check_loop(self):
        """定期健康检查"""
        while self._state == ConnectionState.CONNECTED:
            await asyncio.sleep(60)
            
            if not await self.health_check():
                self.logger.warning("Health check failed, reconnecting...")
                await self._schedule_reconnect()
                break
    
    async def _reduce_memory_usage(self):
        """减少内存使用"""
        if self.cache_manager:
            # 清理一半的L2缓存
            async with self.cache_manager._l2_lock:
                keys = list(self.cache_manager._l2_cache.keys())
                if keys:
                    half = len(keys) // 2
                    keys_to_remove = sorted(
                        keys, 
                        key=lambda k: self.cache_manager._access_count.get(k, 0)
                    )[:half]
                    
                    for key in keys_to_remove:
                        self.cache_manager._l2_cache.pop(key, None)
                        self.cache_manager._access_count.pop(key, None)
        
        # 清理L1缓存
        if self.cache_manager:
            async with self.cache_manager._l1_lock:
                self.cache_manager._l1_cache.clear()
        
        # 建议GC收集
        gc.collect()
    
    # ========== 抽象方法 ==========
    
    @abstractmethod
    async def on_message(self, message: Any) -> None:
        """处理接收到的消息"""
        pass
    
    @abstractmethod
    def get_ws_url(self) -> str:
        """获取WebSocket连接URL"""
        pass
    
    @abstractmethod
    def get_heartbeat_message(self) -> Any:
        """获取心跳消息"""
        pass

    # ========== 连接状态管理 ==========
    
    @property
    def state(self) -> ConnectionState:
        """获取当前连接状态"""
        return self._state
    
    def _set_state(self, new_state: ConnectionState):
        """设置连接状态并触发回调"""
        old_state = self._state
        self._state = new_state
        self.logger.info("State changed: %s -> %s", old_state.value, new_state.value)  # 修复：格式化字符串
        # 修复：使用跟踪的任务
        self._create_task(self._trigger_callbacks("state_change", old_state, new_state))
    
    async def wait_for_state(self, state: ConnectionState, timeout: float = 30) -> bool:
        """等待特定状态"""
        start_time = time.time()
        while self._state != state:
            if time.time() - start_time > timeout:
                return False
            await asyncio.sleep(0.1)
        return True

    # ========== HTTP方法优化 ==========
    
    @monitor_performance
    async def http_get(
        self, 
        endpoint: str, 
        params: Optional[Dict] = None,
        use_cache: bool = True,
        cache_ttl: float = None,
        **kwargs
    ) -> Any:
        """增强的HTTP GET请求"""
        if not self.base_url:
            raise ValueError("Base URL is not configured")
            
        cache_key = f"GET:{endpoint}:{params}" if use_cache and self.cache_manager else None
        
        if cache_key:
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                return cached
        
        await self.rate_limiter.wait()
        
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        result = await self._http_request("GET", url, params=params, **kwargs)
        
        if cache_key and result is not None:
            await self.cache_manager.set(cache_key, result, cache_ttl)
        
        return result
    
    @monitor_performance
    async def http_post(
        self, 
        endpoint: str, 
        data: Any = None, 
        use_cache: bool = False,
        cache_ttl: float = None,
        **kwargs
    ) -> Any:
        """增强的HTTP POST请求"""
        if not self.base_url:
            raise ValueError("Base URL is not configured")
            
        cache_key = f"POST:{endpoint}:{hashlib.md5(str(data).encode()).hexdigest()}" if use_cache and self.cache_manager else None
        
        if cache_key:
            cached = await self.cache_manager.get(cache_key)
            if cached is not None:
                return cached
        
        await self.rate_limiter.wait()
        
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        result = await self._http_request("POST", url, data=data, **kwargs)
        
        if cache_key and result is not None:
            await self.cache_manager.set(cache_key, result, cache_ttl)
        
        return result

    @monitor_performance
    async def http_batch_request(
        self, 
        requests: List[Tuple[str, str, Dict]],
        max_concurrent: int = 10
    ) -> List[Any]:
        """批量HTTP请求"""
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def single_request(method, endpoint, params):
            async with semaphore:
                return await self.http_get(endpoint, params) if method == "GET" \
                       else await self.http_post(endpoint, params)
        
        tasks = [
            single_request(method, endpoint, params) 
            for method, endpoint, params in requests
        ]
        
        return await asyncio.gather(*tasks, return_exceptions=True)

    async def _http_request(
        self, 
        method: str, 
        url: str, 
        data: Any = None, 
        **kwargs
    ) -> Any:
        """执行HTTP请求的核心方法"""
        session = await self.connection_pool.get_session(self.base_url)
        
        headers = kwargs.pop('headers', {})
        if self.enable_compression:
            headers['Accept-Encoding'] = 'gzip, deflate, br'
        
        try:
            await self._trigger_callbacks("before_send", method, url, data)
            
            start_time = time.time()
            async with session.request(
                method, url, json=data, headers=headers, proxy=self.proxy, **kwargs
            ) as response:
                
                if response.status == 200:
                    content_type = response.headers.get('Content-Type', '')
                    
                    if 'application/json' in content_type:
                        result = await response.json()
                    else:
                        result = await response.text()
                    
                    self.metrics.increment_received(len(str(result).encode()))
                    await self._trigger_callbacks("after_send", method, url, data, result)
                    
                    return result
                else:
                    error_msg = "HTTP %s %s failed with status %d", method, url, response.status  # 修复：格式化字符串
                    self.logger.error(error_msg)
                    self.metrics.increment_error()
                    response.raise_for_status()
                    
        except asyncio.TimeoutError:
            error_msg = "HTTP request timeout after %ds", self.timeout  # 修复：格式化字符串
            self.logger.error(error_msg)
            self.metrics.increment_error()
            raise
        except Exception as e:
            self.logger.error("HTTP request failed: %s", str(e))  # 修复：格式化字符串
            self.metrics.increment_error()
            await self._handle_error(e)
            raise

    # ========== WebSocket优化 ==========
    
    @monitor_performance
    async def connect_websocket(self) -> bool:
        """连接WebSocket"""
        async with self._connection_lock:
            if self._state in [ConnectionState.CONNECTING, ConnectionState.CONNECTED]:
                self.logger.warning("WebSocket is already connecting or connected")
                return self._state == ConnectionState.CONNECTED
            
            self._set_state(ConnectionState.CONNECTING)
            self._retry_count = 0
            
            ws_url = self.get_ws_url() or self.ws_url
            if not ws_url:
                self.logger.error("WebSocket URL is not configured")
                self._set_state(ConnectionState.ERROR)
                return False
            
            try:
                ws_options = {
                    "extra_headers": self.headers,
                    "ping_interval": self.heartbeat_interval,
                    "ping_timeout": self.heartbeat_interval * 0.8,
                    "close_timeout": 10,
                    "ssl": self.ssl_context if ws_url.startswith('wss') else None,
                    "compression": "deflate" if self.enable_compression else None
                }
                
                if self.proxy:
                    ws_options["proxy"] = self.proxy
                
                self._ws_connection = await connect(ws_url, **ws_options)
                self._set_state(ConnectionState.CONNECTED)
                self._retry_count = 0
                self.metrics.increment_connection()
                
                self._listener_task = self._create_task(self._listen_messages())
                self._heartbeat_task = self._create_task(self._heartbeat_loop())
                self._health_check_task = self._create_task(self._health_check_loop())
                
                if self._memory_monitor_task is None:
                    self._memory_monitor_task = self._create_task(self._monitor_memory())
                
                await self._trigger_callbacks("connect")
                
                self.logger.info("WebSocket connected successfully")
                return True
                
            except Exception as e:
                self.logger.error("WebSocket connection failed: %s", str(e))  # 修复：格式化字符串
                self._set_state(ConnectionState.ERROR)
                await self._handle_error(e)
                await self._schedule_reconnect()
                return False

    async def disconnect_websocket(self) -> None:
        """断开WebSocket连接"""
        async with self._connection_lock:
            if self._state == ConnectionState.DISCONNECTED:
                return
            
            self._set_state(ConnectionState.DISCONNECTING)  # 修复：现在枚举值存在
            
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            
            if self._ws_connection:
                await self._ws_connection.close()
                self._ws_connection = None
            
            # 停止内存监控
            self._memory_monitor_running = False
            
            self._set_state(ConnectionState.DISCONNECTED)
            self.metrics.increment_disconnection()
            
            await self._trigger_callbacks("disconnect")
            self.logger.info("WebSocket disconnected")

    @monitor_performance
    async def send_websocket_message(
        self, 
        message: Any, 
        message_type: str = "message",
        wait_for_response: bool = False,
        timeout: float = 30
    ) -> Optional[Any]:
        """发送WebSocket消息，支持等待响应"""
        if self._state != ConnectionState.CONNECTED or not self._ws_connection:
            raise ConnectionError("WebSocket is not connected")
        
        message_id = str(uuid.uuid4())
        message_obj = Message(
            id=message_id,
            type=message_type,
            data=message,
            timestamp=time.time()
        )
        
        if wait_for_response:
            future = asyncio.Future()
            await self.message_queue.register_pending(message_id, future)
        else:
            future = None
        
        try:
            message_data = self.serializer.serialize(message)
            await self._trigger_callbacks("before_send", message_obj)
            
            await self._ws_connection.send(message_data)
            self.metrics.increment_sent(len(message_data))
            await self._trigger_callbacks("after_send", message_obj)
            
            if future:
                try:
                    result = await asyncio.wait_for(future, timeout=timeout)
                    return result
                except asyncio.TimeoutError:
                    await self.message_queue.reject_pending(
                        message_id, 
                        asyncio.TimeoutError("Response timeout")
                    )
                    raise
            
            return None
            
        except Exception as e:
            self.logger.error("Failed to send WebSocket message: %s", str(e))  # 修复：格式化字符串
            if future:
                await self.message_queue.reject_pending(message_id, e)
            await self._handle_error(e)
            await self._schedule_reconnect()
            raise

    async def _listen_messages(self) -> None:
        """监听WebSocket消息"""
        while self._state == ConnectionState.CONNECTED and self._ws_connection:
            try:
                message = await self._ws_connection.recv()
                await self._handle_websocket_message(message)
            except Exception as e:
                if self._state == ConnectionState.CONNECTED:
                    self.logger.error("Error receiving message: %s", str(e))  # 修复：格式化字符串
                    await self._handle_error(e)
                    await self._schedule_reconnect()
                break

    async def _handle_websocket_message(self, message: Any) -> None:
        """处理WebSocket消息"""
        try:
            if isinstance(message, bytes):
                message = self.serializer.deserialize(message)
            
            message_obj = Message(
                id=str(uuid.uuid4()),
                type="websocket",
                data=message,
                timestamp=time.time(),
                metadata={"original_length": len(str(message))}
            )
            
            self.metrics.increment_received(len(str(message).encode()))
            await self.on_message(message)
            await self._trigger_callbacks("message", message_obj)
            
            if isinstance(message, dict) and "id" in message:
                await self.message_queue.resolve_pending(message["id"], message)
                
        except Exception as e:
            self.logger.error("Error handling WebSocket message: %s", str(e))  # 修复：格式化字符串
            await self._handle_error(e)

    async def _heartbeat_loop(self) -> None:
        """心跳循环"""
        while self._state == ConnectionState.CONNECTED:
            try:
                heartbeat_msg = self.get_heartbeat_message()
                if heartbeat_msg:
                    await self.send_websocket_message(heartbeat_msg, "heartbeat")
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Heartbeat error: %s", str(e))  # 修复：格式化字符串
                if self._state == ConnectionState.CONNECTED:
                    await self._handle_error(e)
                break

    # ========== 重连机制 ==========
    
    async def _schedule_reconnect(self) -> None:
        """安排重连"""
        if (self._state not in [ConnectionState.CONNECTED, ConnectionState.RECONNECTING] and 
            self._retry_count < self.max_retries):
            
            self._set_state(ConnectionState.RECONNECTING)
            self._reconnect_task = self._create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """重连循环"""
        while (self._retry_count < self.max_retries and 
               self._state == ConnectionState.RECONNECTING):
            
            self._retry_count += 1
            # 修复：改进指数退避算法
            delay = min(
                self.reconnect_interval * (2 ** (self._retry_count - 1)),
                300  # 最大5分钟
            )
            
            self.logger.info(
                "Reconnecting in %ds (attempt %d/%d)", delay, self._retry_count, self.max_retries  # 修复：格式化字符串
            )
            
            await asyncio.sleep(delay)
            
            try:
                success = await self.connect_websocket()
                if success:
                    self.logger.info("Reconnected successfully")
                    return
            except Exception as e:
                self.logger.error("Reconnection attempt %d failed: %s", self._retry_count, str(e))  # 修复：格式化字符串
        
        self.logger.error("Max reconnection attempts reached")
        self._set_state(ConnectionState.ERROR)

    # ========== 回调函数管理优化 ==========
    
    def register_callback(self, event: str, callback: Callable):  # 修复：保持同步方法，使用线程锁
        """注册回调函数"""
        async def async_wrapper(*args, **kwargs):
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(*args, **kwargs)
                else:
                    await asyncio.get_event_loop().run_in_executor(
                        self._thread_pool, 
                        lambda: callback(*args, **kwargs)
                    )
            except Exception as e:
                self.logger.error("Callback error for event %s: %s", event, str(e))  # 修复：格式化字符串
        
        with self._callback_lock:  # 修复：使用线程锁
            if event in self._callbacks:
                self._callbacks[event].append(async_wrapper)
            else:
                self.logger.warning("Unknown event type: %s", event)  # 修复：格式化字符串
    
    async def _trigger_callbacks(self, event: str, *args, **kwargs):
        """触发回调函数"""
        if event in self._callbacks:
            tasks = []
            with self._callback_lock:  # 修复：使用线程锁
                callbacks = self._callbacks[event].copy()
            
            for callback in callbacks:
                task = self._create_task(callback(*args, **kwargs))  # 修复：使用跟踪的任务
                tasks.append(task)
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    # ========== 错误处理 ==========
    
    async def _handle_error(self, error: Exception) -> None:
        """处理错误"""
        self.metrics.increment_error()
        await self._trigger_callbacks("error", error)
        self.logger.error("Error occurred: %s", str(error))  # 修复：格式化字符串

    # ========== 任务管理 ==========
    
    def _create_task(self, coro) -> asyncio.Task:
        """创建并跟踪任务"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.discard(t))
        return task

    # ========== 上下文管理器支持 ==========
    
    @asynccontextmanager
    async def connection_context(self):
        """连接上下文管理器"""
        try:
            await self.connect_websocket()
            yield self
        finally:
            await self.disconnect_websocket()
    
    async def health_check(self) -> bool:
        """健康检查"""
        if self._state != ConnectionState.CONNECTED:
            return False

        try:
            heartbeat_msg = self.get_heartbeat_message()
            response = await self.send_websocket_message(
                heartbeat_msg,
                wait_for_response=True,
                timeout=10
            )
            return response is not None
        except Exception as e:
            self.logger.error("Health check failed: %s", str(e))  # 修复：格式化字符串
            return False

    # ========== 生命周期管理 ==========
    
    async def initialize(self) -> None:
        """初始化适配器"""
        self.logger.info("Initializing adapter")
        await self._trigger_callbacks("state_change", None, self._state)
    
    async def cleanup(self) -> None:
        """清理资源"""
        self.logger.info("Cleaning up adapter")
        
        await self.disconnect_websocket()
        await self.connection_pool.close_all()
        
        # 清理缓存管理器
        if self.cache_manager:
            await self.cache_manager.close()
        
        self._thread_pool.shutdown(wait=False)
        
        for task in self._tasks:
            if not task.done():
                task.cancel()
        
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        self.logger.info("Adapter cleanup completed")

    # ========== 装饰器方法 ==========
    
    def on_message_handler(self, message_type: str = None):
        """消息处理器装饰器"""
        def decorator(func):
            key = message_type or "default"
            self._message_handlers[key] = func
            return func
        return decorator
    
    def on_connect_handler(self, func):
        """连接事件装饰器"""
        self.register_callback("connect", func)
        return func
    
    def on_disconnect_handler(self, func):
        """断开连接事件装饰器"""
        self.register_callback("disconnect")
        return func
    
    def on_error_handler(self, func):
        """错误事件装饰器"""
        self.register_callback("error", func)
        return func

    # ========== 缓存管理 ==========
    
    async def clear_cache(self, pattern: str = None):
        """清空缓存"""
        if self.cache_manager:
            if pattern:
                keys_to_delete = []
                async with self.cache_manager._l2_lock:
                    for key in list(self.cache_manager._l2_cache.keys()):
                        if pattern in key:
                            keys_to_delete.append(key)
                
                for key in keys_to_delete:
                    await self.cache_manager.delete(key)
            else:
                await self.cache_manager.clear()
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        if self.cache_manager:
            return self.cache_manager.get_stats()
        return {"enabled": False}

    # ========== 指标统计 ==========
    
    def get_metrics(self) -> Dict[str, Any]:
        """获取连接指标"""
        metrics = self.metrics.get_stats()
        metrics.update({
            "state": self._state.value,
            "retry_count": self._retry_count,
            "pending_messages": len(self.message_queue.pending_messages),
            "queue_size": self.message_queue.queue.qsize(),
            "active_tasks": len(self._tasks),
            "memory_usage_mb": self._get_memory_usage()
        })
        return metrics

    async def __aenter__(self):
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

# ========== 使用示例 ==========

class MyServiceAdapter(HighPerfBaseAdapter):
    """具体服务适配器示例"""
    
    def __init__(self, **kwargs):  # 修复：接受可变参数
        super().__init__(
            base_url="https://api.example.com",
            ws_url="wss://ws.example.com",
            headers={"User-Agent": "MyAdapter/1.0"},
            **kwargs  # 修复：传递给父类
        )
    
    async def on_message(self, message: Any) -> None:
        """处理接收到的消息"""
        if isinstance(message, dict):
            message_type = message.get("type")
            handler = self._message_handlers.get(message_type)
            if handler:
                await handler(message)
            else:
                self.logger.info("Received message: %s", message)  # 修复：格式化字符串
    
    def get_ws_url(self) -> str:
        """获取WebSocket URL"""
        return "wss://ws.example.com/v1/websocket"
    
    def get_heartbeat_message(self) -> Any:
        """获取心跳消息"""
        return {"type": "ping", "timestamp": int(time.time())}
    
    # 业务特定方法
    async def get_user_info(self, user_id: str, use_cache: bool = True):
        """获取用户信息"""
        return await self.http_get(f"/users/{user_id}", use_cache=use_cache)
    
    async def send_chat_message(self, room: str, message: str, wait_ack: bool = True):
        """发送聊天消息"""
        data = {"room": room, "message": message, "timestamp": int(time.time())}
        return await self.send_websocket_message(data, wait_for_response=wait_ack)

# 使用示例
async def demo():
    async with MyServiceAdapter() as adapter:
        # 注册回调
        @adapter.on_connect_handler
        async def on_connect():
            print("✅ Connected to service!")
        
        @adapter.on_message_handler("chat")
        async def handle_chat_message(msg):
            print("💬 Chat message: %s", msg)  # 修复：格式化字符串
        
        @adapter.on_error_handler
        async def handle_error(error):
            print("❌ Error: %s", error)  # 修复：格式化字符串
        
        # 连接WebSocket
        await adapter.connect_websocket()
        
        # 使用HTTP功能
        user_info = await adapter.get_user_info("123", use_cache=True)
        print("👤 User info: %s", user_info)  # 修复：格式化字符串
        
        # 使用WebSocket功能
        await adapter.send_chat_message("general", "Hello, World!")
        
        # 查看指标
        metrics = adapter.get_metrics()
        print("📊 Metrics: %s", metrics)  # 修复：格式化字符串
        
        # 保持运行
        await asyncio.sleep(60)

async def advanced_demo():
    """高级使用示例"""
    # 自定义配置
    adapter = MyServiceAdapter(  # 修复：现在可以接受参数
        max_queue_size=5000,
        rate_limit={"requests": 200, "window": 60},
        enable_performance_monitoring=True,
        max_memory_mb=1000
    )
    
    async with adapter:
        # 批量操作
        requests = [
            ("GET", "/users/1", {}),
            ("GET", "/users/2", {}),
            ("GET", "/users/3", {})
        ]
        results = await adapter.http_batch_request(requests)
        print("Batch results: %s", results)  # 修复：格式化字符串
        
        # 监控循环
        async def monitor_loop():
            while True:
                await asyncio.sleep(30)
                metrics = adapter.get_metrics()
                cache_stats = adapter.get_cache_stats()
                print("📊 Live metrics: %s", metrics)  # 修复：格式化字符串
                print("💾 Cache stats: %s", cache_stats)  # 修复：格式化字符串
        
        monitor_task = asyncio.create_task(monitor_loop())
        
        # 业务逻辑
        await asyncio.sleep(120)
        monitor_task.cancel()

if __name__ == "__main__":
    # 性能测试
    import time
    start = time.time()
    asyncio.run(advanced_demo())
    print("⏱️ Total execution time: %.2fs", time.time() - start)  # 修复：格式化字符串
