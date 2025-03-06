import asyncio, os
from fastapi import FastAPI, HTTPException
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Depends, Security

from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List, Dict, Any, Union
import psutil
import time
import uuid
import math
import logging
import traceback
from enum import Enum
from dataclasses import dataclass
from crawl4ai import AsyncWebCrawler, CrawlResult, CacheMode
from crawl4ai.config import MIN_WORD_THRESHOLD
from crawl4ai.extraction_strategy import (
    LLMExtractionStrategy,
    CosineStrategy,
    JsonCssExtractionStrategy,
)
from sqlalchemy import create_engine, Column, String, Float, JSON, Integer, Enum as SQLEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.ext.asyncio import AsyncSession
import json
from datetime import datetime

__location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class CrawlerType(str, Enum):
    BASIC = "basic"
    LLM = "llm"
    COSINE = "cosine"
    JSON_CSS = "json_css"


class ExtractionConfig(BaseModel):
    type: CrawlerType
    params: Dict[str, Any] = {}


class ChunkingStrategy(BaseModel):
    type: str
    params: Dict[str, Any] = {}


class ContentFilter(BaseModel):
    type: str = "bm25"
    params: Dict[str, Any] = {}


class CrawlRequest(BaseModel):
    urls: Union[HttpUrl, List[HttpUrl]]
    word_count_threshold: int = MIN_WORD_THRESHOLD
    extraction_config: Optional[ExtractionConfig] = None
    chunking_strategy: Optional[ChunkingStrategy] = None
    content_filter: Optional[ContentFilter] = None
    js_code: Optional[List[str]] = None
    wait_for: Optional[str] = None
    css_selector: Optional[str] = None
    screenshot: bool = False
    magic: bool = False
    extra: Optional[Dict[str, Any]] = {}
    session_id: Optional[str] = None
    cache_mode: Optional[CacheMode] = CacheMode.ENABLED
    priority: int = Field(default=5, ge=1, le=10)
    ttl: Optional[int] = 3600
    crawler_params: Dict[str, Any] = {}


@dataclass
class TaskInfo:
    id: str
    status: TaskStatus
    result: Optional[Union[CrawlResult, List[CrawlResult]]] = None
    error: Optional[str] = None
    created_at: float = time.time()
    ttl: int = 3600
    request: Optional[CrawlRequest] = None


class ResourceMonitor:
    def __init__(self, max_concurrent_tasks: int = 10):
        self.max_concurrent_tasks = max_concurrent_tasks
        self.memory_threshold = 0.85
        self.cpu_threshold = 0.90
        self._last_check = 0
        self._check_interval = 1  # seconds
        self._last_available_slots = max_concurrent_tasks

    async def get_available_slots(self) -> int:
        current_time = time.time()
        if current_time - self._last_check < self._check_interval:
            return self._last_available_slots

        mem_usage = psutil.virtual_memory().percent / 100
        cpu_usage = psutil.cpu_percent() / 100

        memory_factor = max(
            0, (self.memory_threshold - mem_usage) / self.memory_threshold
        )
        cpu_factor = max(0, (self.cpu_threshold - cpu_usage) / self.cpu_threshold)

        self._last_available_slots = math.floor(
            self.max_concurrent_tasks * min(memory_factor, cpu_factor)
        )
        self._last_check = current_time

        return self._last_available_slots


Base = declarative_base()

class DBTask(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    status = Column(SQLEnum(TaskStatus))
    result = Column(JSON, nullable=True)
    error = Column(String, nullable=True)
    created_at = Column(Float)
    ttl = Column(Integer)
    request = Column(JSON, nullable=True)


class TaskManager:
    def __init__(self, cleanup_interval: int = 300):
        self.cleanup_interval = cleanup_interval
        self.cleanup_task = None
        self.use_postgres = os.getenv("ENABLE_POSTGRES_TASK_MANAGEMENT", "").lower() == "true"

        # Initialize storage based on configuration
        if self.use_postgres:
            self.engine = None
            self.async_session = None
        else:
            self.tasks: Dict[str, TaskInfo] = {}

        self.high_priority = asyncio.PriorityQueue()
        self.low_priority = asyncio.PriorityQueue()

    async def start(self):
        if self.use_postgres:
            database_url = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost/crawl4ai")
            self.engine = create_async_engine(database_url)

            # Create tables
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            self.async_session = sessionmaker(
                self.engine, class_=AsyncSession, expire_on_commit=False
            )

        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        if self.use_postgres and self.engine:
            await self.engine.dispose()

    async def add_task(self, task_id: str, priority: int, ttl: int, request: Optional[CrawlRequest] = None) -> None:
        if self.use_postgres:
            request_dict = request.model_dump()
            # Convert HttpUrl and CacheMode to string
            if isinstance(request_dict['urls'], list):
                request_dict['urls'] = [str(url) for url in request_dict['urls']]
            else:
                request_dict['urls'] = str(request_dict['urls'])

            if request_dict.get('cache_mode'):
                request_dict['cache_mode'] = request_dict['cache_mode'].value

            async with self.async_session() as session:
                task = DBTask(
                    id=task_id,
                    status=TaskStatus.PENDING,
                    created_at=time.time(),
                    ttl=ttl,
                    request=request_dict if request else None
                )
                session.add(task)
                await session.commit()
        else:
            task_info = TaskInfo(id=task_id, status=TaskStatus.PENDING, ttl=ttl)
            self.tasks[task_id] = task_info

        queue = self.high_priority if priority > 5 else self.low_priority
        await queue.put((-priority, task_id))

    async def get_next_task(self) -> Optional[str]:
        try:
            # Try high priority first
            _, task_id = await asyncio.wait_for(self.high_priority.get(), timeout=0.1)
            return task_id
        except asyncio.TimeoutError:
            try:
                # Then try low priority
                _, task_id = await asyncio.wait_for(self.low_priority.get(), timeout=0.1)
                return task_id
            except asyncio.TimeoutError:
                return None

    async def update_task(
        self, task_id: str, status: TaskStatus, result: Any = None, error: str = None
    ):
        if self.use_postgres:
            async with self.async_session() as session:
                task = await session.get(DBTask, task_id)
                if task:
                    task.status = status
                    if result:
                        def serialize_datetime(obj):
                            if isinstance(obj, datetime):
                                return obj.isoformat()
                            return obj

                        # Convert result to dict first
                        if isinstance(result, list):
                            result_dict = [r.model_dump() for r in result]
                        else:
                            result_dict = result.model_dump()

                        # Handle datetime objects
                        task.result = json.loads(
                            json.dumps(result_dict, default=serialize_datetime)
                        )
                    task.error = error
                    await session.commit()
        else:
            if task_id in self.tasks:
                task_info = self.tasks[task_id]
                task_info.status = status
                task_info.result = result
                task_info.error = error

    async def get_task(self, task_id: str) -> Optional[TaskInfo]:
        if self.use_postgres:
            async with self.async_session() as session:
                task = await session.get(DBTask, task_id)
                if task:
                    result = task.result
                    if result:
                        if isinstance(result, list):
                            result = [CrawlResult(**r) for r in result]
                        else:
                            result = CrawlResult(**result)

                    return TaskInfo(
                        id=task.id,
                        status=task.status,
                        result=result,
                        error=task.error,
                        created_at=task.created_at,
                        ttl=task.ttl,
                        request=CrawlRequest(**task.request) if task.request else None
                    )
                return None
        else:
            return self.tasks.get(task_id)

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                current_time = time.time()

                if not self.use_postgres:
                    expired_tasks = [
                        task_id
                        for task_id, task in self.tasks.items()
                        if current_time - task.created_at > task.ttl
                        and task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]
                    ]
                    for task_id in expired_tasks:
                        del self.tasks[task_id]

            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")


class CrawlerPool:
    def __init__(self, max_size: int = 10):
        self.max_size = max_size
        self.active_crawlers: Dict[AsyncWebCrawler, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, **kwargs) -> AsyncWebCrawler:
        async with self._lock:
            # Clean up inactive crawlers
            current_time = time.time()
            inactive = [
                crawler
                for crawler, last_used in self.active_crawlers.items()
                if current_time - last_used > 600  # 10 minutes timeout
            ]
            for crawler in inactive:
                await crawler.__aexit__(None, None, None)
                del self.active_crawlers[crawler]

            # Create new crawler if needed
            if len(self.active_crawlers) < self.max_size:
                crawler = AsyncWebCrawler(**kwargs)
                await crawler.__aenter__()
                self.active_crawlers[crawler] = current_time
                return crawler

            # Reuse least recently used crawler
            crawler = min(self.active_crawlers.items(), key=lambda x: x[1])[0]
            self.active_crawlers[crawler] = current_time
            return crawler

    async def release(self, crawler: AsyncWebCrawler):
        async with self._lock:
            if crawler in self.active_crawlers:
                self.active_crawlers[crawler] = time.time()

    async def cleanup(self):
        async with self._lock:
            for crawler in list(self.active_crawlers.keys()):
                await crawler.__aexit__(None, None, None)
            self.active_crawlers.clear()


class CrawlerService:
    def __init__(self, max_concurrent_tasks: int = 10):
        self.resource_monitor = ResourceMonitor(max_concurrent_tasks)
        self.task_manager = TaskManager()
        self.crawler_pool = CrawlerPool(max_concurrent_tasks)
        self._processing_task = None

    async def start(self):
        await self.task_manager.start()
        self._processing_task = asyncio.create_task(self._process_queue())

    async def stop(self):
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        await self.task_manager.stop()
        await self.crawler_pool.cleanup()

    def _create_extraction_strategy(self, config: ExtractionConfig):
        if not config:
            return None

        if config.type == CrawlerType.LLM:
            return LLMExtractionStrategy(**config.params)
        elif config.type == CrawlerType.COSINE:
            return CosineStrategy(**config.params)
        elif config.type == CrawlerType.JSON_CSS:
            return JsonCssExtractionStrategy(**config.params)
        return None

    async def submit_task(self, request: CrawlRequest) -> str:
        task_id = str(uuid.uuid4())
        await self.task_manager.add_task(task_id, request.priority, request.ttl or 3600, request)

        # Store request data with task
        if not self.task_manager.use_postgres:
            self.task_manager.tasks[task_id].request = request

        return task_id

    async def _process_queue(self):
        while True:
            try:
                available_slots = await self.resource_monitor.get_available_slots()
                if False and available_slots <= 0:
                    await asyncio.sleep(1)
                    continue

                task_id = await self.task_manager.get_next_task()
                if not task_id:
                    await asyncio.sleep(1)
                    continue

                task_info = await self.task_manager.get_task(task_id)
                if not task_info:
                    continue

                request = task_info.request
                await self.task_manager.update_task(task_id, TaskStatus.PROCESSING)

                try:
                    crawler = await self.crawler_pool.acquire(**request.crawler_params)

                    extraction_strategy = self._create_extraction_strategy(
                        request.extraction_config
                    )

                    if isinstance(request.urls, list):
                        results = await crawler.arun_many(
                            urls=[str(url) for url in request.urls],
                            word_count_threshold=MIN_WORD_THRESHOLD,
                            extraction_strategy=extraction_strategy,
                            js_code=request.js_code,
                            wait_for=request.wait_for,
                            css_selector=request.css_selector,
                            screenshot=request.screenshot,
                            magic=request.magic,
                            session_id=request.session_id,
                            cache_mode=request.cache_mode,
                            **request.extra,
                        )
                    else:
                        results = await crawler.arun(
                            url=str(request.urls),
                            extraction_strategy=extraction_strategy,
                            js_code=request.js_code,
                            wait_for=request.wait_for,
                            css_selector=request.css_selector,
                            screenshot=request.screenshot,
                            magic=request.magic,
                            session_id=request.session_id,
                            cache_mode=request.cache_mode,
                            **request.extra,
                        )

                    # Force cleanup
                    import gc
                    gc.collect()

                    await self.crawler_pool.release(crawler)
                    await self.task_manager.update_task(
                        task_id, TaskStatus.COMPLETED, results
                    )

                except Exception as e:
                    logger.error(f"Error processing task {task_id}: {str(e)}")
                    await self.task_manager.update_task(
                        task_id, TaskStatus.FAILED, error=str(e)
                    )

            except Exception as e:
                logger.error(f"Error in queue processing: {str(e)}")
                await asyncio.sleep(1)


app = FastAPI(title="Crawl4AI API")

# CORS configuration
origins = ["*"]  # Allow all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # List of origins that are allowed to make requests
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# API token security
security = HTTPBearer()
CRAWL4AI_API_TOKEN = os.getenv("CRAWL4AI_API_TOKEN")


async def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not CRAWL4AI_API_TOKEN:
        return credentials  # No token verification if CRAWL4AI_API_TOKEN is not set
    if credentials.credentials != CRAWL4AI_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return credentials


def secure_endpoint():
    """Returns security dependency only if CRAWL4AI_API_TOKEN is set"""
    return Depends(verify_token) if CRAWL4AI_API_TOKEN else None


# Check if site directory exists
if os.path.exists(__location__ + "/site"):
    # Mount the site directory as a static directory
    app.mount("/mkdocs", StaticFiles(directory="site", html=True), name="mkdocs")

site_templates = Jinja2Templates(directory=__location__ + "/site")

crawler_service = CrawlerService()


@app.on_event("startup")
async def startup_event():
    await crawler_service.start()


@app.on_event("shutdown")
async def shutdown_event():
    await crawler_service.stop()


@app.get("/")
def read_root():
    if os.path.exists(__location__ + "/site"):
        return RedirectResponse(url="/mkdocs")
    # Return a json response
    return {"message": "Crawl4AI API service is running"}


@app.post("/crawl", dependencies=[secure_endpoint()] if CRAWL4AI_API_TOKEN else [])
async def crawl(request: CrawlRequest) -> Dict[str, str]:
    task_id = await crawler_service.submit_task(request)
    return {"task_id": task_id}


@app.get(
    "/task/{task_id}", dependencies=[secure_endpoint()] if CRAWL4AI_API_TOKEN else []
)
async def get_task_status(task_id: str):
    task_info = await crawler_service.task_manager.get_task(task_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="Task not found")

    response = {
        "status": task_info.status,
        "created_at": task_info.created_at,
    }

    if task_info.status == TaskStatus.COMPLETED:
        # Convert CrawlResult to dict for JSON response
        if isinstance(task_info.result, list):
            response["results"] = [result.model_dump() for result in task_info.result]
        else:
            response["result"] = task_info.result.model_dump()
    elif task_info.status == TaskStatus.FAILED:
        response["error"] = task_info.error

    return response


@app.post("/crawl_sync", dependencies=[secure_endpoint()] if CRAWL4AI_API_TOKEN else [])
async def crawl_sync(request: CrawlRequest) -> Dict[str, Any]:
    task_id = await crawler_service.submit_task(request)

    # Wait up to 60 seconds for task completion
    for _ in range(60):
        task_info = crawler_service.task_manager.get_task(task_id)
        if not task_info:
            raise HTTPException(status_code=404, detail="Task not found")

        if task_info.status == TaskStatus.COMPLETED:
            # Return same format as /task/{task_id} endpoint
            if isinstance(task_info.result, list):
                return {
                    "status": task_info.status,
                    "results": [result.model_dump() for result in task_info.result],
                }
            return {"status": task_info.status, "result": task_info.result.model_dump()}

        if task_info.status == TaskStatus.FAILED:
            raise HTTPException(status_code=500, detail=task_info.error)

        await asyncio.sleep(1)

    # If we get here, task didn't complete within timeout
    raise HTTPException(status_code=408, detail="Task timed out")


@app.post(
    "/crawl_direct", dependencies=[secure_endpoint()] if CRAWL4AI_API_TOKEN else []
)
async def crawl_direct(request: CrawlRequest) -> Dict[str, Any]:
    try:
        crawler = await crawler_service.crawler_pool.acquire(**request.crawler_params)
        extraction_strategy = crawler_service._create_extraction_strategy(
            request.extraction_config
        )

        try:
            if isinstance(request.urls, list):
                results = await crawler.arun_many(
                    urls=[str(url) for url in request.urls],
                    extraction_strategy=extraction_strategy,
                    js_code=request.js_code,
                    wait_for=request.wait_for,
                    css_selector=request.css_selector,
                    screenshot=request.screenshot,
                    magic=request.magic,
                    cache_mode=request.cache_mode,
                    session_id=request.session_id,
                    **request.extra,
                )
                return {"results": [result.model_dump() for result in results]}
            else:
                result = await crawler.arun(
                    url=str(request.urls),
                    extraction_strategy=extraction_strategy,
                    js_code=request.js_code,
                    wait_for=request.wait_for,
                    css_selector=request.css_selector,
                    screenshot=request.screenshot,
                    magic=request.magic,
                    cache_mode=request.cache_mode,
                    session_id=request.session_id,
                    **request.extra,
                )
                return {"result": result.model_dump()}
        finally:
            await crawler_service.crawler_pool.release(crawler)
    except Exception as e:
        logger.error(f"Error in direct crawl: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    available_slots = await crawler_service.resource_monitor.get_available_slots()
    memory = psutil.virtual_memory()
    return {
        "status": "healthy",
        "available_slots": available_slots,
        "memory_usage": memory.percent,
        "cpu_usage": psutil.cpu_percent(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=11235)
