import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select

import api.enums
import api.globals as g
from api.conf import Config, Credentials
from api.conf.config import ConfigModel
from api.conf.credentials import CredentialsModel
from api.database import get_async_session_context
from api.enums import RevChatStatus
from api.models.db import User, RevConversation
from api.schema import LogFilterOptions, SystemInfo, RequestStatistics
from api.users import current_super_user
from utils.logger import get_logger

logger = get_logger(__name__)
config = Config()
credentials = Credentials()

router = APIRouter()

check_users_cache = None
check_users_cache_last_update_time: datetime | None = None

CACHE_DURATION_SECONDS = 0  # currently do not cache, for there seems no significant performance improvement


async def check_users(refresh_cache: bool = False):
    global check_users_cache
    global check_users_cache_last_update_time

    if refresh_cache:
        check_users_cache = None
        check_users_cache_last_update_time = None
    if check_users_cache is not None and check_users_cache_last_update_time is not None:
        if check_users_cache_last_update_time > datetime.utcnow() - timedelta(seconds=CACHE_DURATION_SECONDS):
            # logger.debug("Using cached check_users result")
            return check_users_cache
    # logger.debug("Refreshing check_users cache")
    check_users_cache_last_update_time = datetime.utcnow()
    async with get_async_session_context() as session:
        users = await session.execute(select(User))
        users = users.scalars().all()
    queueing_count = 0
    active_user_in_5m = 0
    active_user_in_1h = 0
    active_user_in_1d = 0
    current_time = datetime.now().astimezone(tz=timezone.utc)
    for user in users:
        if not user.last_active_time:
            continue
        if user.rev_chat_status == RevChatStatus.queueing:
            queueing_count += 1
        if user.is_superuser:  # 管理员不计入在线人数
            continue
        if user.last_active_time > current_time - timedelta(minutes=5):
            active_user_in_5m += 1
        if user.last_active_time > current_time - timedelta(hours=1):
            active_user_in_1h += 1
        if user.last_active_time > current_time - timedelta(days=1):
            active_user_in_1d += 1

    check_users_cache = (active_user_in_5m, active_user_in_1h, active_user_in_1d, queueing_count, users)
    return check_users_cache


@router.get("/system/info", tags=["system"], response_model=SystemInfo)
async def get_system_info(_user: User = Depends(current_super_user)):
    active_user_in_5m, active_user_in_1h, active_user_in_1d, queueing_count, users = await check_users(
        refresh_cache=True)
    async with get_async_session_context() as session:
        conversations = await session.execute(select(RevConversation))
        conversations = conversations.scalars().all()
    result = SystemInfo(
        startup_time=g.startup_time,
        total_user_count=len(users),
        total_conversation_count=len(conversations),
        valid_conversation_count=len([c for c in conversations if c.is_valid]),
    )
    return result


FAKE_REQ_START_TIMESTAMP = 1672502400  # 2023-01-01 00:00:00


def make_fake_requests_count(total=100, max=500):
    result = {}
    start_stage = FAKE_REQ_START_TIMESTAMP // config.stats.request_counts_interval
    for i in range(total):
        result[start_stage + i] = [random.randint(0, max), [1]]
    return result


def make_fake_ask_records(total=100, days=2):
    result = []
    model_names = list(api.enums.models.RevChatModels)
    for i in range(total):
        ask_time = random.random() * 60 + 1
        total_time = ask_time + random.random() * 30
        result.append([
            [
                # random.randint(1, 10),  # user_id
                1,
                model_names[random.randint(0, len(model_names) - 1)],  # model_name
                ask_time,
                total_time
            ],
            FAKE_REQ_START_TIMESTAMP + random.random() * 60 * 60 * 24 * days,  # ask_time
        ])
    return result


@router.get("/system/stats/request", tags=["system"], response_model=RequestStatistics)
async def get_request_statistics(_user: User = Depends(current_super_user)):
    result = RequestStatistics(
        request_counts_interval=config.stats.request_counts_interval,
        request_counts=dict(g.request_log_counter.counter),
        # request_counts=make_fake_requests_count(20, 500),
        ask_records=list(g.ask_log_queue.queue)
        # ask_records=make_fake_ask_records(3000, 7)
    )
    return result


def read_last_n_lines(file_path, n, exclude_key_words=None):
    if exclude_key_words is None:
        exclude_key_words = []
    try:
        with open(file_path, "r") as f:
            lines = f.readlines()[::-1]
    except FileNotFoundError:
        return [f"File not found: {file_path}"]
    last_n_lines = []
    for line in lines:
        if len(last_n_lines) >= n:
            break
        if any([line.find(key_word) != -1 for key_word in exclude_key_words]):
            continue
        last_n_lines.append(line)
    return last_n_lines[::-1]


@router.post("/system/logs/server", tags=["system"])
async def get_server_logs(_user: User = Depends(current_super_user), options: LogFilterOptions = LogFilterOptions()):
    lines = read_last_n_lines(
        g.server_log_filename,
        options.max_lines,
        options.exclude_keywords
    )
    return lines


@router.get("/system/config", tags=["system"], response_model=ConfigModel)
async def get_config(_user: User = Depends(current_super_user)):
    return config.model()


@router.put("/system/config", tags=["system"], response_model=ConfigModel)
async def update_config(config_model: ConfigModel, _user: User = Depends(current_super_user)):
    config.update(config_model)
    config.save()
    return config.model()


@router.get("/system/credentials", tags=["system"], response_model=CredentialsModel)
async def get_credentials(_user: User = Depends(current_super_user)):
    # TODO: 安全防范
    return credentials.model()


@router.put("/system/credentials", tags=["system"], response_model=CredentialsModel)
async def update_credentials(credentials_model: CredentialsModel, _user: User = Depends(current_super_user)):
    credentials.update(credentials_model)
    credentials.save()
    return credentials.model()
