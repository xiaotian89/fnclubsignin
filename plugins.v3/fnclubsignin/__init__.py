# -*- coding: utf-8 -*-
"""飞牛论坛签到插件 - MoviePilot V3

每天自动打卡领飞牛币。
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime
from typing import Any, Optional

import httpx2
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import EventType, NotificationChannel


class FnClubSignin(_PluginBase):
    """飞牛论坛每日自动签到。"""

    plugin_name = "飞牛论坛签到"
    plugin_desc = "飞牛论坛每天自动打卡领飞牛币。"
    plugin_icon = "https://club.fnnas.com/favicon.ico"
    plugin_version = "1.8.0"
    plugin_author = "xiaotian"
    author_url = "https://club.fnnas.com"
    plugin_config_prefix = "fnnassignin_"
    plugin_order = 20
    auth_level = 1

    # 站点常量
    _base_url = "https://club.fnnas.com"
    _sign_page = "/plugin.php?id=zqlj_sign"
    _login_api = (
        "/member.php?mod=logging&action=login&loginsubmit=yes"
        "&infloat=yes&lssubmit=yes&inajax=1"
    )
    # 随机 UA 池：模拟不同浏览器的真实 UA，避免固定 UA 的脚本特征
    _user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    # 人类化浏览的页面池（打卡前"顺路逛"的页面）
    _browse_pages = [
        "/",
        "/forum.php",
        "/forum-46-1.html",
        "/forum.php?mod=forumdisplay&fid=46",
    ]

    _enabled = False
    _username = ""
    _password = ""
    _cookie = ""
    _cron = "0 8 * * *"
    _notify = True
    _delay_seconds = 1800
    _humanize = True
    _onlyonce = False
    _scheduler = None
