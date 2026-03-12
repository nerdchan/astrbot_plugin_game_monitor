"""
Discord Game Monitor Plugin for AstrBot - 完整事件驅動架構

功能：
1. on_presence_update 監聽
   - 記錄 game_start_time 和 current_game
   - 25% 隨機觸發 → 立刻喚醒 LLM（帶 Tool Calling 執行網搜）

2. 後台定時檢查 (每 10 分鐘)
   - 計算游玩時間
   - 120 分鐘超時觸發 → 強制提醒（需要 warned_today = False）

3. Tool Calling 整合
   - 系統指令強制 Agent 先使用聯網搜尋技能
   - 再以 Persona 語氣生成回應

Author: AstrBot Community
Version: 0.3.1
"""

import logging
import json
import asyncio
import os
import random
import re
from typing import Optional
from datetime import datetime, timedelta, date as date_type

import discord
from discord.ext import tasks
from astrbot.core.agent.tool import ToolSet

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger as astr_logger, AstrBotConfig

logger = logging.getLogger(__name__)


@register(
    name="astrbot_plugin_game_monitor",
    author="Melantilla.",
    desc="主動監控 Discord 使用者的實時遊戲狀態，並透過人格生成符合人設的互動回覆。",
    version="0.3.1"
)
class GameMonitorPlugin(Star):
    """
    Discord 遊戲監控插件 - 完整架構
    
    事件流：
    ┌─ on_presence_update (Discord Gateway 推送，無延遲)
    │  ├─ 記錄 game_start_time, current_game
    │  └─ 25% 概率 → 呼叫 _trigger_llm_recognition() (帶 Tool Calling)
    │
    ├─ Background tasks.loop (每 10 分鐘執行)
    │  ├─ 計算 game_duration_minutes  
    │  ├─ 若 >= 120 分鐘 && warned_today == False
    │  └─ → 呼叫 _trigger_llm_timeout_warning()
    │
    └─ /查遊戲 指令 (被動查詢)
       ├─ 讀 state.json
       └─ 呼叫 LLM 生成回覆
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.state_file = self._get_state_file_path()
        
        # 配置參數
        self.discord_bot_token = self.config.get("discord_bot_token", "")
        self.target_guild_id = self.config.get("target_guild_id", "")
        self.target_discord_id = self.config.get("target_discord_id", "")
        self.target_channel_id = self.config.get("target_channel_id", "")
        self.use_web_search = self.config.get("use_web_search", False)
        self.linkage_mode = self._normalize_choice(
            self.config.get("linkage_mode", "auto"),
            {"auto", "fallback", "strict"},
            "auto",
        )
        self.persona_source = self._normalize_choice(
            self.config.get("persona_source", "current_persona"),
            {"current_persona", "soul_file", "external_persona_plugin", "custom_prompt_profile"},
            "current_persona",
        )
        self.persona_plugin_name = str(self.config.get("persona_plugin_name", "") or "").strip()
        self.search_source = self._normalize_choice(
            self.config.get("search_source", "auto"),
            {"auto", "astrbot_native_tools", "external_search_plugin", "agent_skill", "mixed"},
            "auto",
        )
        self.search_plugin_name = str(self.config.get("search_plugin_name", "") or "").strip()
        self.search_tool_preferences = self._parse_name_list(self.search_plugin_name)
        self.search_style = self._normalize_choice(
            self.config.get("search_style", "hybrid"),
            {"formal", "community", "hybrid"},
            "hybrid",
        )
        self.delivery_mode = self._normalize_choice(
            self.config.get("delivery_mode", "auto"),
            {"auto", "channel_push", "reply_only", "both"},
            "auto",
        )
        
        # 後台 Discord Bot
        self.discord_bot = None
        self.discord_client = None
        self.background_task = None
        self._background_check_task = None
        self._presence_poll_task = None
        self._presence_listener_registered = False
        
        # Tool Calling 狀態
        self._recognition_triggered_today = set()  # 記錄今天已觸發識別的遊戲名
        
        logger.info(
            f"[GameMonitor] 插件初始化。狀態文件: {self.state_file}"
        )
        
        if not self.discord_bot_token:
            logger.warning("[GameMonitor] Discord Bot Token 未配置，事件監聽功能將不可用。")
        if not self.target_guild_id or not self.target_discord_id:
            logger.warning("[GameMonitor] 目標 Guild ID 或 User ID 未配置。")

    def _normalize_choice(self, raw_value, allowed_values, default_value: str) -> str:
        """Normalize a config enum-like value to avoid invalid branch behavior."""
        value = str(raw_value or "").strip().lower()
        if value in allowed_values:
            return value
        return default_value

    def _parse_name_list(self, raw_value: str) -> list[str]:
        """Parse comma/newline separated config names into a compact list."""
        if not raw_value:
            return []

        parts = re.split(r"[,\n]+", str(raw_value))
        return [part.strip() for part in parts if part.strip()]

    def _get_state_file_path(self) -> str:
        """取得 state.json 的絕對路徑"""
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(plugin_dir, "data", "state.json")

    def _default_state(self) -> dict:
        """state.json 的預設結構，確保事件欄位不缺失"""
        return {
            "current_game": None,
            "game_start_time": None,
            "last_game_update": None,
            "timestamp": None,
            "update_count": 0,
            "bot_status": "initializing",
            "warned_today": False,
            "warning_count_today": 0,
            "last_warning_time": None,
            "game_duration_minutes": 0,
            "today_date": date_type.today().isoformat(),
            "last_discord_update": None,
            "last_bot_update": None,
            "last_presence_event": None,
        }

    def _now_iso(self) -> str:
        return datetime.now().isoformat()

    def _parse_discord_id(self, raw_value: str) -> Optional[int]:
        """Parse Discord ID from plain digits or mention-like formats."""
        if raw_value is None:
            return None

        value = str(raw_value).strip()
        if not value:
            return None

        if value.isdigit():
            return int(value)

        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))

        return None

    def _resolve_discord_token(self) -> str:
        """優先使用插件配置；若為空則嘗試從 AstrBot 全局配置解析。"""
        if self.discord_bot_token:
            return self.discord_bot_token

        cfg = None
        try:
            if hasattr(self.context, "get_config"):
                cfg = self.context.get_config()
            elif hasattr(self.context, "_config"):
                cfg = self.context._config
        except Exception:
            cfg = None

        if not cfg:
            return ""

        # 兼容多種配置結構
        candidates = [
            ("platform", "discord", "token"),
            ("platform_settings", "discord", "token"),
            ("platforms", "discord", "token"),
            ("discord", "token"),
        ]

        for path in candidates:
            cur = cfg
            ok = True
            for key in path:
                if hasattr(cur, "get"):
                    cur = cur.get(key)
                else:
                    ok = False
                    break
                if cur is None:
                    ok = False
                    break
            if ok and isinstance(cur, str) and cur.strip():
                logger.info(f"[GameMonitor] 已使用 AstrBot 全局配置中的 Discord Token（路徑: {'/'.join(path)}）")
                return cur.strip()

        return ""

    def _persist_state(self, state: dict):
        """寫入插件狀態文件"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def _get_astrbot_discord_client(self):
        """從 AstrBot 上下文獲取已存在的 Discord client。"""
        try:
            manager_candidates = [
                getattr(self.context, "platform_manager", None),
                getattr(self.context, "_platform_manager", None),
                getattr(self.context, "platform_mgr", None),
                getattr(self.context, "_platform_mgr", None),
            ]

            for pm in manager_candidates:
                if not pm or not hasattr(pm, "platform_insts"):
                    continue

                for inst in pm.platform_insts:
                    client = getattr(inst, "client", None)
                    if client is None and hasattr(inst, "get_client"):
                        try:
                            client = inst.get_client()
                        except Exception:
                            client = None

                    if not hasattr(inst, "meta"):
                        continue

                    try:
                        meta = inst.meta()
                    except Exception:
                        continue

                    if getattr(meta, "name", "") == "discord" and client is not None:
                        return client

            # 後備：若 context 提供按名稱查平台的方法
            if hasattr(self.context, "get_platform"):
                try:
                    inst = self.context.get_platform("discord")
                    client = getattr(inst, "client", None)
                    if client is None and hasattr(inst, "get_client"):
                        client = inst.get_client()
                    if client is not None:
                        return client
                except Exception:
                    pass

            # 後備：舊版上下文可能直接暴露 platform_adapter
            adapter = getattr(self.context, "platform_adapter", None)
            if adapter is not None and hasattr(adapter, "client"):
                return adapter.client
        except Exception as e:
            logger.warning(f"[GameMonitor] 獲取 AstrBot Discord client 失敗: {e}")
        return None

    def _get_discord_intents_snapshot(self) -> dict:
        """回傳當前 Discord client intents 的可讀快照。"""
        default_snapshot = {
            "presences": None,
            "members": None,
            "message_content": None,
        }

        client = getattr(self, "discord_client", None)
        intents = getattr(client, "intents", None) if client is not None else None
        if intents is None:
            return default_snapshot

        return {
            "presences": bool(getattr(intents, "presences", False)),
            "members": bool(getattr(intents, "members", False)),
            "message_content": bool(getattr(intents, "message_content", False)),
        }

    async def _attach_presence_listener(self) -> bool:
        """嘗試掛載 Presence listener，成功返回 True。"""
        try:
            client = self._get_astrbot_discord_client()
            if client is None:
                return False

            self.discord_client = client

            if not hasattr(self.discord_client, "add_listener"):
                self._update_state_bot_status("listener_failed")
                logger.error("[GameMonitor] Discord client 不支持 add_listener")
                return False

            if not self._presence_listener_registered:
                self.discord_client.add_listener(
                    self._on_presence_update,
                    "on_presence_update",
                )
                self._presence_listener_registered = True
                logger.info("[GameMonitor] 已掛載 on_presence_update listener 到 AstrBot Discord client")

            intents_snapshot = self._get_discord_intents_snapshot()
            if intents_snapshot.get("presences") is False:
                logger.warning(
                    "[GameMonitor] 檢測到 intents.presences=False；這會導致 last_presence_event/last_discord_update 持續為 null。"
                )

            self._update_state_bot_status("connected")
            return True
        except Exception as e:
            logger.error(f"[GameMonitor] 掛載 Presence listener 失敗: {str(e)}", exc_info=True)
            return False

    async def _retry_attach_presence_listener(self):
        """延遲重試，避免初始化時機差導致 adapter_missing。"""
        while not self._presence_listener_registered:
            await asyncio.sleep(15)
            ok = await self._attach_presence_listener()
            if ok:
                logger.info("[GameMonitor] 延遲重試成功，Presence listener 已就緒")
                return

    @filter.command("查遊戲")
    async def query_game_status(self, event: AstrMessageEvent):
        """被動查詢指令 - 讀 state.json 然後生成 Persona 回覆"""
        sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else ""
        logger.info(f"[GameMonitor] 收到查遊戲指令，觸發人: {sender_name or 'unknown'}")
        
        try:
            game_info = self._read_game_state()
            
            if not game_info or not game_info.get("current_game"):
                logger.info("[GameMonitor] 無遊戲記錄")
                yield event.plain_result("📭 目前沒有遊戲記錄。請先開啟遊戲，並等待 Discord 更新狀態。")
                return
            
            game_name = game_info["current_game"]
            prompt = self._assemble_casual_prompt(game_name)
            llm_response = await self._call_llm_with_prompt(event, prompt)
            
            if llm_response:
                yield event.plain_result(llm_response)
            else:
                yield event.plain_result(f"🎮 玩家正在進行: {game_name}")
        
        except Exception as e:
            logger.error(f"[GameMonitor] 查詢失敗: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 查詢失敗: {str(e)}")

    @filter.command("遊戲監控診斷")
    async def diagnose_monitor_status(self, event: AstrMessageEvent):
        """輸出 Presence 監控診斷資訊，協助定位為何 state 未更新。"""
        sender_name = event.get_sender_name() if hasattr(event, "get_sender_name") else ""
        logger.info(f"[GameMonitor] 收到遊戲監控診斷指令，觸發人: {sender_name or 'unknown'}")

        try:
            if not self._presence_listener_registered:
                await self._attach_presence_listener()

            state = self._read_game_state() or self._default_state()

            parsed_user_id = self._parse_discord_id(self.target_discord_id)
            parsed_guild_id = self._parse_discord_id(self.target_guild_id)

            last_presence = state.get("last_presence_event")
            last_discord = state.get("last_discord_update")
            last_bot = state.get("last_bot_update")
            bot_status = state.get("bot_status", "unknown")
            current_game = state.get("current_game") or "(none)"
            intents_snapshot = self._get_discord_intents_snapshot()
            platform_manager = getattr(self.context, "platform_manager", None)
            platform_insts = getattr(platform_manager, "platform_insts", []) if platform_manager else []
            discord_inst_count = 0
            for inst in platform_insts:
                try:
                    if hasattr(inst, "meta") and getattr(inst.meta(), "name", "") == "discord":
                        discord_inst_count += 1
                except Exception:
                    continue

            presence_age = self._format_elapsed_from_iso(last_presence)
            discord_age = self._format_elapsed_from_iso(last_discord)
            bot_age = self._format_elapsed_from_iso(last_bot)

            warnings = []
            if parsed_user_id is None:
                warnings.append("target_discord_id 無效，事件會被忽略")
            if not self._presence_listener_registered:
                warnings.append("Presence listener 尚未註冊")
            if bot_status in {"adapter_missing", "listener_failed", "start_failed"}:
                warnings.append(f"bot_status={bot_status}，初始化異常")
            if last_presence is None:
                warnings.append("尚未收到任何 Presence 事件")
            if intents_snapshot.get("presences") is False:
                warnings.append("Discord intents.presences=False，Gateway 不會推送活動狀態")
            if intents_snapshot.get("members") is False:
                warnings.append("Discord intents.members=False，部分成員事件/快取將不可用")

            warn_text = "\n".join([f"- {w}" for w in warnings]) if warnings else "- 無明顯異常"

            report = (
                "[GameMonitor Diagnostics]\n"
                f"bot_status: {bot_status}\n"
                f"listener_registered: {self._presence_listener_registered}\n"
                f"context.has_platform_manager: {platform_manager is not None}\n"
                f"context.platform_insts_count: {len(platform_insts)}\n"
                f"context.discord_inst_count: {discord_inst_count}\n"
                f"target_discord_id(raw): {self.target_discord_id or '(empty)'}\n"
                f"target_discord_id(parsed): {parsed_user_id}\n"
                f"target_guild_id(raw): {self.target_guild_id or '(empty)'}\n"
                f"target_guild_id(parsed): {parsed_guild_id}\n"
                f"client_intents.presences: {intents_snapshot.get('presences')}\n"
                f"client_intents.members: {intents_snapshot.get('members')}\n"
                f"client_intents.message_content: {intents_snapshot.get('message_content')}\n"
                f"current_game: {current_game}\n"
                f"last_presence_event: {last_presence} (age: {presence_age})\n"
                f"last_discord_update: {last_discord} (age: {discord_age})\n"
                f"last_bot_update: {last_bot} (age: {bot_age})\n"
                f"update_count: {state.get('update_count', 0)}\n"
                "warnings:\n"
                f"{warn_text}"
            )

            yield event.plain_result(report)
        except Exception as e:
            logger.error(f"[GameMonitor] 診斷失敗: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 診斷失敗: {str(e)}")

    @filter.command("遊戲監控強制認知")
    async def force_recognition_trigger(self, event: AstrMessageEvent):
        """強制觸發認知建立流程，便於測試（忽略 25% 隨機門檻）。"""
        try:
            state = self._read_game_state() or self._default_state()
            game_name = state.get("current_game")
            if not game_name:
                yield event.plain_result("❌ 目前沒有正在進行的遊戲，無法測試認知觸發。")
                return

            llm_text = await self._trigger_llm_recognition(game_name, event=event)
            self._recognition_triggered_today.add(game_name)
            if llm_text:
                yield event.plain_result(f"✅ 已強制觸發認知流程，遊戲: {game_name}\n\n{llm_text}")
            else:
                yield event.plain_result(
                    "⚠️ 已觸發認知流程，但未取得 LLM 回覆。"
                    "請檢查 provider 設定，或用 /查遊戲 驗證模型可用性。"
                )
        except Exception as e:
            logger.error(f"[GameMonitor] 強制認知測試失敗: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 強制認知測試失敗: {str(e)}")

    @filter.command("遊戲監控強制超時")
    async def force_timeout_trigger(self, event: AstrMessageEvent):
        """強制構造超時條件並執行檢查，便於測試 120 分鐘提醒邏輯。"""
        try:
            state = self._read_game_state() or self._default_state()
            game_name = state.get("current_game")
            if not game_name:
                yield event.plain_result("❌ 目前沒有正在進行的遊戲，無法測試超時觸發。")
                return

            state["warned_today"] = False
            state["game_duration_minutes"] = max(int(state.get("game_duration_minutes", 0)), 120)
            state["last_bot_update"] = self._now_iso()
            self._persist_state(state)

            await self._check_game_duration()

            latest = self._read_game_state() or state
            yield event.plain_result(
                "✅ 已執行強制超時檢查\n"
                f"game: {game_name}\n"
                f"game_duration_minutes: {latest.get('game_duration_minutes')}\n"
                f"warned_today: {latest.get('warned_today')}\n"
                f"warning_count_today: {latest.get('warning_count_today')}\n"
                f"last_warning_time: {latest.get('last_warning_time')}"
            )
        except Exception as e:
            logger.error(f"[GameMonitor] 強制超時測試失敗: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 強制超時測試失敗: {str(e)}")

    @filter.command("遊戲監控重置警告")
    async def reset_warning_flags(self, event: AstrMessageEvent):
        """重置當日警告旗標，便於重複測試超時流程。"""
        try:
            state = self._read_game_state() or self._default_state()
            state["warned_today"] = False
            state["warning_count_today"] = 0
            state["last_warning_time"] = None
            state["last_bot_update"] = self._now_iso()
            self._persist_state(state)
            yield event.plain_result("✅ 已重置 warned_today / warning_count_today / last_warning_time")
        except Exception as e:
            logger.error(f"[GameMonitor] 重置警告旗標失敗: {str(e)}", exc_info=True)
            yield event.plain_result(f"❌ 重置警告旗標失敗: {str(e)}")

    def _format_elapsed_from_iso(self, iso_time: Optional[str]) -> str:
        """將 ISO 時間字串轉為可讀的相對時間。"""
        if not iso_time:
            return "never"

        try:
            normalized = iso_time.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
            delta = now - parsed
            total_seconds = int(max(delta.total_seconds(), 0))

            if total_seconds < 60:
                return f"{total_seconds}s"
            if total_seconds < 3600:
                return f"{total_seconds // 60}m {total_seconds % 60}s"
            return f"{total_seconds // 3600}h {(total_seconds % 3600) // 60}m"
        except Exception:
            return "invalid-time"

    def _read_game_state(self) -> Optional[dict]:
        """讀取 state.json"""
        try:
            if not os.path.exists(self.state_file):
                default_state = self._default_state()
                self._persist_state(default_state)
                logger.info(f"[GameMonitor] 已建立預設 state.json: {self.state_file}")
                return default_state
            
            with open(self.state_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            # 向後相容：補齊舊版 state 缺少的欄位
            state = self._default_state()
            state.update(loaded)
            return state
        
        except Exception as e:
            logger.error(f"[GameMonitor] 讀取 state.json 失敗: {str(e)}")
            return None

    def _write_game_state(self, game_name: Optional[str], game_start_time: Optional[datetime] = None):
        """
        寫入 state.json
        
        Args:
            game_name: 遊戲名稱（若為 None，表示停止遊戲）
            game_start_time: 遊戲開始時間（若為 None，表示現在開始）
        """
        try:
            now = datetime.now()
            now_iso = now.isoformat()
            today = date_type.today()
            
            existing = self._read_game_state() or self._default_state()
            
            # 檢查日期是否變更，若變更則重置警告狀態
            existing_today = existing.get("today_date")
            if existing_today and existing_today != today.isoformat():
                logger.info("[GameMonitor] 日期已變換，重置 warned_today 狀態")
                existing["warned_today"] = False
                existing["warning_count_today"] = 0
            
            existing["today_date"] = today.isoformat()
            
            if game_name:
                # 開始遊戲
                existing["current_game"] = game_name
                existing["game_start_time"] = (game_start_time or now).isoformat()
                existing["last_game_update"] = now_iso
                existing["timestamp"] = now.strftime("%Y-%m-%d %H:%M:%S")
                existing["update_count"] = existing.get("update_count", 0) + 1
                existing["game_duration_minutes"] = 0
            else:
                # 停止遊戲
                existing["current_game"] = None
                existing["game_start_time"] = None
                existing["game_duration_minutes"] = 0

            # Discord 事件更新心跳
            existing["last_discord_update"] = now_iso
            existing["last_presence_event"] = now_iso
            existing["last_bot_update"] = now_iso
            existing["bot_status_updated"] = now_iso
            
            self._persist_state(existing)
            
            logger.info(f"[GameMonitor] 已更新 state.json: {game_name}")
        
        except Exception as e:
            logger.error(f"[GameMonitor] 寫入 state.json 失敗: {str(e)}")

    def _sync_state_from_presence_snapshot(self, game_name: Optional[str]):
        """以快照方式同步 Presence 狀態，避免只依賴 on_presence_update 事件。"""
        try:
            now = datetime.now()
            now_iso = now.isoformat()
            state = self._read_game_state() or self._default_state()

            prev_game = state.get("current_game")

            # 只要成功讀到目標成員快照，就更新 Presence 時間戳
            state["last_discord_update"] = now_iso
            state["last_presence_event"] = now_iso
            state["last_bot_update"] = now_iso
            state["bot_status_updated"] = now_iso

            if game_name:
                if prev_game != game_name or not state.get("game_start_time"):
                    state["current_game"] = game_name
                    state["game_start_time"] = now_iso
                    state["update_count"] = state.get("update_count", 0) + 1
                    state["game_duration_minutes"] = 0
                state["last_game_update"] = now_iso
                state["timestamp"] = now.strftime("%Y-%m-%d %H:%M:%S")
            else:
                if prev_game is not None:
                    state["current_game"] = None
                    state["game_start_time"] = None
                    state["game_duration_minutes"] = 0
                    state["last_game_update"] = now_iso
                    state["timestamp"] = now.strftime("%Y-%m-%d %H:%M:%S")

            self._persist_state(state)
        except Exception as e:
            logger.error(f"[GameMonitor] 快照同步狀態失敗: {str(e)}")

    async def _poll_presence_snapshot(self):
        """後備輪詢：每分鐘主動讀取目標成員活動，彌補事件未觸發情況。"""
        if not self.discord_client:
            return

        target_id = self._parse_discord_id(self.target_discord_id)
        target_guild = self._parse_discord_id(self.target_guild_id)
        if target_id is None:
            return

        try:
            guild = None
            member = None

            if target_guild is not None and hasattr(self.discord_client, "get_guild"):
                guild = self.discord_client.get_guild(target_guild)

            if guild is None:
                guilds = getattr(self.discord_client, "guilds", []) or []
                for g in guilds:
                    try:
                        m = g.get_member(target_id)
                    except Exception:
                        m = None
                    if m is not None:
                        guild = g
                        member = m
                        break

            if guild is not None and member is None:
                member = guild.get_member(target_id)
                if member is None and hasattr(guild, "fetch_member"):
                    try:
                        member = await guild.fetch_member(target_id)
                    except Exception:
                        member = None

            if member is None:
                return

            game_name = None
            activities = getattr(member, "activities", []) or []
            for activity in activities:
                if hasattr(activity, "type") and activity.type == discord.ActivityType.playing:
                    game_name = (getattr(activity, "name", None) or "").strip() or None
                    if game_name:
                        break

            self._sync_state_from_presence_snapshot(game_name)
        except Exception as e:
            logger.debug(f"[GameMonitor] 輪詢目標 Presence 失敗: {str(e)}")

    def _update_game_duration(self):
        """計算並更新遊戲時長"""
        try:
            state = self._read_game_state()
            if not state or not state.get("game_start_time"):
                return
            
            start_time = datetime.fromisoformat(state["game_start_time"])
            duration_minutes = int((datetime.now() - start_time).total_seconds() / 60)
            
            state["game_duration_minutes"] = duration_minutes

            self._persist_state(state)
            
            logger.debug(f"[GameMonitor] 遊玩時長: {duration_minutes} 分鐘")
        
        except Exception as e:
            logger.error(f"[GameMonitor] 更新遊戲時長失敗: {str(e)}")

    async def initialize(self):
        """插件啟動（AstrBot 生命周期鉤子）"""
        logger.info("[GameMonitor] initialize() 被調用，準備掛載 Discord 事件監聽...")

        # 啟動時先落一筆 bot update，避免 state 長時間為 null
        self._update_state_bot_status("starting")
        
        try:
            attached = await self._attach_presence_listener()
            if not attached:
                logger.warning("[GameMonitor] 尚未找到 AstrBot Discord client，進入延遲重試模式。")
                self._update_state_bot_status("adapter_missing")
                if self.background_task is None or self.background_task.done():
                    self.background_task = asyncio.create_task(self._retry_attach_presence_listener())

            # 啟動定時檢查任務
            self._background_check_task = tasks.loop(minutes=10)(self._check_game_duration)
            if not self._background_check_task.is_running():
                self._background_check_task.start()

            # 後備輪詢：避免 Discord 未推送 presence 變更時完全無更新
            self._presence_poll_task = tasks.loop(minutes=1)(self._poll_presence_snapshot)
            if not self._presence_poll_task.is_running():
                self._presence_poll_task.start()
        except Exception as e:
            logger.error(f"[GameMonitor] initialize 失敗: {str(e)}", exc_info=True)
            self._update_state_bot_status("start_failed")

    async def terminate(self):
        """插件關閉（AstrBot 生命周期鉤子）"""
        logger.info("[GameMonitor] terminate() 被調用，正在卸載監聽...")
        try:
            if self._background_check_task and hasattr(self._background_check_task, "is_running"):
                if self._background_check_task.is_running():
                    self._background_check_task.cancel()

            if self._presence_poll_task and hasattr(self._presence_poll_task, "is_running"):
                if self._presence_poll_task.is_running():
                    self._presence_poll_task.cancel()

            # 不關閉 AstrBot 的主 client，只移除自己的 listener
            if self.discord_client and self._presence_listener_registered and hasattr(self.discord_client, "remove_listener"):
                self.discord_client.remove_listener(
                    self._on_presence_update,
                    "on_presence_update",
                )
                self._presence_listener_registered = False

            if self.background_task and not self.background_task.done():
                self.background_task.cancel()

            self._update_state_bot_status("terminated")
        except Exception as e:
            logger.error(f"[GameMonitor] terminate 失敗: {str(e)}", exc_info=True)

    async def _on_presence_update(self, before, after):
        """Phase 1: 監聽 Presence 變化（掛載到 AstrBot 主 client）"""
        try:
            target_id = self._parse_discord_id(self.target_discord_id)
            target_guild = self._parse_discord_id(self.target_guild_id)
        except Exception:
            return

        if target_id is None:
            logger.warning("[GameMonitor] target_discord_id 無效，無法處理 Presence 事件")
            return

        try:
            # 兼容不同對象結構
            user_obj = getattr(after, "user", after)
            user_id = getattr(user_obj, "id", None)
            guild_obj = getattr(after, "guild", None)
            guild_id = getattr(guild_obj, "id", None)

            if user_id != target_id:
                return

            # 某些 Presence 事件可能缺少 guild；僅在兩邊都有值時才做嚴格比對。
            if target_guild is not None and guild_id is not None and guild_id != target_guild:
                return

            game_name = None
            activities = getattr(after, "activities", []) or []
            for activity in activities:
                if hasattr(activity, "type") and activity.type == discord.ActivityType.playing:
                    game_name = (getattr(activity, "name", None) or "").strip() or None
                    if game_name:
                        break

            if game_name:
                logger.info(f"[GameMonitor] 🎮 目標用戶開始進行: {game_name}")
                self._sync_state_from_presence_snapshot(game_name)

                if random.random() < 0.25 and game_name not in self._recognition_triggered_today:
                    logger.info(f"[GameMonitor] 🎲 觸發 25% 隨機認知建立: {game_name}")
                    llm_text = await self._trigger_llm_recognition(game_name)
                    if llm_text and self._should_send_proactive_from_presence():
                        await self._send_proactive_message(llm_text)
                    self._recognition_triggered_today.add(game_name)
            else:
                logger.info("[GameMonitor] 目標用戶停止遊戲")
                self._sync_state_from_presence_snapshot(None)
        except Exception as e:
            logger.error(f"[GameMonitor] Presence 處理失敗: {str(e)}")

    async def _check_game_duration(self):
        """
        Phase 2: 後台定時檢查 (每 10 分鐘)
        
        - 計算當前遊玩時長
        - 若 >= 120 分鐘 && warned_today == False
        - 觸發 LLM 超時警告
        """
        try:
            self._update_game_duration()
            state = self._read_game_state()
            
            if not state or not state.get("current_game"):
                return  # 沒有遊戲在進行
            
            duration = state.get("game_duration_minutes", 0)
            
            if duration >= 120 and not state.get("warned_today", False):
                logger.warning(f"[GameMonitor] ⏰ 遊玩超過 120 分鐘: {duration} 分")
                game_name = state["current_game"]
                await self._trigger_llm_timeout_warning(game_name, duration)
                
                # 標記已警告
                state["warned_today"] = True
                state["last_warning_time"] = datetime.now().isoformat()
                state["warning_count_today"] = state.get("warning_count_today", 0) + 1

                self._persist_state(state)
        
        except Exception as e:
            logger.error(f"[GameMonitor] 定時檢查失敗: {str(e)}")

    async def _trigger_llm_recognition(self, game_name: str, event: Optional[AstrMessageEvent] = None) -> Optional[str]:
        """
        認知建立 Prompt - 使用 Tool Calling
        
        系統指令強制 Agent：
        1. 先使用聯網搜尋技能了解遊戲
        2. 再以 Persona 語氣進行回應
        """
        try:
            strict_reason = self._get_strict_block_reason(event)
            if strict_reason:
                logger.warning(f"[GameMonitor] strict 模式阻擋觸發: {strict_reason}")
                return None

            system_instruction = self._build_recognition_prompt(game_name)
            
            provider_id = ""
            if event is not None:
                try:
                    provider_id = await self.context.get_current_chat_provider_id(
                        umo=event.unified_msg_origin
                    )
                except Exception:
                    provider_id = ""

            if not provider_id:
                using_provider = self.context.get_using_provider()
                if using_provider is not None:
                    provider_id = using_provider.meta().id

            if not provider_id:
                logger.warning("[GameMonitor] 認知建立失敗：找不到可用的聊天模型 provider")
                return None

            prompt = system_instruction
            execution_mode, reason = self._decide_search_execution_mode(event)
            logger.info(
                f"[GameMonitor] 認知觸發策略: linkage_mode={self.linkage_mode}, "
                f"persona_source={self.persona_source}, search_source={self.search_source}, "
                f"execution_mode={execution_mode}, reason={reason}"
            )

            if execution_mode == "tool_loop":
                active_tools = self._get_active_global_tools()
                selected_tools, tool_reason = self._select_tools_for_search(active_tools)
                if selected_tools.empty():
                    logger.warning(
                        f"[GameMonitor] 找不到可用搜尋工具，停止 tool_loop。reason={tool_reason}"
                    )
                    if self.linkage_mode == "strict":
                        return None
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                    )
                else:
                    logger.info(
                        "[GameMonitor] 搜尋工具選擇: %s | reason=%s",
                        [tool.name for tool in getattr(selected_tools, "tools", [])],
                        tool_reason,
                    )
                    llm_resp = await self.context.tool_loop_agent(
                        event=event,
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        tools=selected_tools,
                        max_steps=8,
                        tool_call_timeout=60,
                    )
            else:
                llm_resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                )

            llm_text = (getattr(llm_resp, "completion_text", "") or "").strip()
            llm_text = self._strip_tool_artifacts(llm_text)
            if not llm_text:
                logger.warning("[GameMonitor] 認知建立完成，但 LLM 回覆為空")
                return None

            logger.info(f"[GameMonitor] 認知建立回覆: {llm_text}")
            return llm_text
            
        except Exception as e:
            logger.error(f"[GameMonitor] 認知建立觸發失敗: {str(e)}")
            return None

    def _build_recognition_prompt(self, game_name: str) -> str:
        """Assemble prompt with selectable persona/search directives."""
        persona_hint = self._build_persona_hint()
        style_hint = {
            "formal": "輸出偏正式且資訊密度高。",
            "community": "輸出採玩家社群口吻，接地氣、可輕鬆吐槽。",
            "hybrid": "先保留重點事實，再改寫成玩家社群口吻。",
        }.get(self.search_style, "先保留重點事實，再改寫成玩家社群口吻。")

        search_hint = ""
        if self.use_web_search:
            preferred_hint = self._build_search_tool_hint()
            search_hint = (
                "\n3️⃣ 盡量先取得遊戲背景資訊後再回覆。"
                "若可以呼叫工具，請優先使用可用搜尋能力。"
                f"{preferred_hint}"
                "不要在最終回答中輸出 <tool_code> 或工具指令標記。"
            )

        return f"""【系統指令】：User 剛剛開啟了一款名為『{game_name}』的遊戲。

請你執行以下步驟：

1️⃣ 先理解這款遊戲大致類型與特色（例如 FPS、RPG、休閒、恐怖等）。

2️⃣ 以你的 Persona 語氣，對 User 說一句簡短的關心或調侃。

{persona_hint}

語氣要求：{style_hint}
{search_hint}

保持簡潔，一句話即可。"""

    def _build_persona_hint(self) -> str:
        """Return persona-source specific prompt guidance."""
        if self.persona_source == "soul_file":
            return "人格來源偏好：若你的執行環境有 SOUL.md 規格，請優先遵循其語氣與人設。"

        if self.persona_source == "external_persona_plugin":
            if self.persona_plugin_name:
                return (
                    f"人格來源偏好：優先參考外部人格插件「{self.persona_plugin_name}」的語氣設定。"
                    "若無法取得其上下文，請維持目前可用 Persona。"
                )
            return "人格來源偏好：優先參考外部人格插件；若無法取得其上下文，請維持目前可用 Persona。"

        if self.persona_source == "custom_prompt_profile":
            return "人格來源偏好：優先維持當前對話中已建立的角色風格與口頭禪。"

        return "人格來源偏好：使用目前會話 Persona。"

    def _build_search_tool_hint(self) -> str:
        """Add explicit tool preference to the prompt when configured."""
        if not self.search_tool_preferences:
            return ""

        joined = ", ".join(self.search_tool_preferences)
        return f"若這些工具可用，請優先使用：{joined}。"

    def _get_strict_block_reason(self, event: Optional[AstrMessageEvent]) -> Optional[str]:
        """Return reason when strict mode cannot satisfy selected linkage sources."""
        if self.linkage_mode != "strict":
            return None

        if self.persona_source == "external_persona_plugin":
            return "external_persona_plugin 目前僅提供提示級聯動，尚未實作通用跨插件人格橋接。"

        if self.use_web_search and event is None and self.search_source in {
            "astrbot_native_tools",
            "mixed",
            "external_search_plugin",
            "agent_skill",
            "auto",
        }:
            return "目前無事件上下文，無法使用 tool_loop_agent。"

        return None

    def _decide_search_execution_mode(self, event: Optional[AstrMessageEvent]) -> tuple[str, str]:
        """Choose tool_loop_agent or llm_generate based on linkage config and runtime context."""
        if not self.use_web_search:
            return "llm_generate", "use_web_search=false"

        if self.search_source in {"external_search_plugin", "agent_skill"}:
            if event is not None:
                reason = (
                    f"search_source={self.search_source} 已配置"
                    f"({self.search_plugin_name or '未填名稱'})，啟用指定工具篩選"
                )
                return "tool_loop", reason
            reason = (
                f"search_source={self.search_source} 已配置"
                f"({self.search_plugin_name or '未填名稱'})，但缺少事件上下文"
            )
            return "llm_generate", reason

        if self.search_source == "mixed":
            if event is not None:
                return "tool_loop", "mixed 模式命中原生工具路徑"
            return "llm_generate", "mixed 模式缺少事件上下文，回退 llm_generate"

        if self.search_source in {"astrbot_native_tools", "auto"}:
            if event is not None:
                return "tool_loop", "事件上下文可用，啟用原生工具鏈"
            return "llm_generate", "無事件上下文，回退 llm_generate"

        return "llm_generate", "未命中可用工具策略，回退 llm_generate"

    def _should_send_proactive_from_presence(self) -> bool:
        """Whether auto recognition from presence should push message to a channel."""
        if self.delivery_mode == "reply_only":
            logger.info("[GameMonitor] delivery_mode=reply_only，Presence 自動觸發不主動推送。")
            return False
        return True

    def _get_active_global_tools(self) -> ToolSet:
        """取得目前啟用中的全域工具集合。"""
        try:
            tool_manager = self.context.get_llm_tool_manager()
            all_tools = tool_manager.get_full_tool_set()
            active_tools = ToolSet()
            for tool in getattr(all_tools, "tools", []):
                if getattr(tool, "active", False):
                    active_tools.add_tool(tool)
            return active_tools
        except Exception as e:
            logger.warning(f"[GameMonitor] 取得全域工具集失敗: {str(e)}")
            return ToolSet()

    def _select_tools_for_search(self, active_tools: ToolSet) -> tuple[ToolSet, str]:
        """Filter tools so tool_loop_agent actually uses the configured search plugin/tool."""
        tools = getattr(active_tools, "tools", [])
        if not tools:
            return ToolSet(), "no_active_tools"

        preferred_names = self.search_tool_preferences[:]
        if self.search_source == "astrbot_native_tools" and not preferred_names:
            preferred_names = [
                "web_search",
                "fetch_url",
                "web_search_tavily",
                "tavily_extract_web_page",
                "web_search_bocha",
            ]

        if not preferred_names:
            return active_tools, "no_preference_configured"

        selected = ToolSet()
        matched_names = []
        for tool in tools:
            if any(self._tool_matches_preference(tool.name, pref) for pref in preferred_names):
                selected.add_tool(tool)
                matched_names.append(tool.name)

        if matched_names:
            return selected, f"matched_preference={matched_names}"

        if self.linkage_mode == "strict":
            return ToolSet(), f"preferred_tools_not_found={preferred_names}"

        return active_tools, f"preferred_tools_not_found={preferred_names}, fallback_to_active_tools"

    def _tool_matches_preference(self, tool_name: str, preference: str) -> bool:
        """Loose match so config can use exact tool names, aliases, or short names."""
        normalized_tool = self._normalize_identifier(tool_name)
        normalized_pref = self._normalize_identifier(preference)
        if not normalized_tool or not normalized_pref:
            return False
        return (
            normalized_tool == normalized_pref
            or normalized_pref in normalized_tool
            or normalized_tool in normalized_pref
        )

    def _normalize_identifier(self, value: str) -> str:
        """Normalize identifiers for fuzzy matching between plugin names and tool names."""
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    def _strip_tool_artifacts(self, text: str) -> str:
        """移除模型輸出的工具標記片段，避免回傳偽工具指令給用戶。"""
        if not text:
            return ""

        cleaned = re.sub(r"<tool_code>.*?</tool_code>", "", text, flags=re.S | re.I)
        cleaned = re.sub(r"【聯網搜尋】", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    async def _send_proactive_message(self, text: str):
        """將主動關心訊息送到指定 Discord 頻道。"""
        channel_id = self._parse_discord_id(self.target_channel_id)
        if channel_id is None:
            if self.delivery_mode == "channel_push":
                logger.warning("[GameMonitor] delivery_mode=channel_push，但 target_channel_id 未配置。")
                return
            logger.info("[GameMonitor] 未配置 target_channel_id，略過主動發送關心訊息")
            return

        if self.discord_client is None:
            logger.warning("[GameMonitor] discord_client 不可用，無法主動發送關心訊息")
            return

        try:
            channel = self.discord_client.get_channel(channel_id)
            if channel is None:
                channel = await self.discord_client.fetch_channel(channel_id)

            if channel is None or not hasattr(channel, "send"):
                logger.warning(f"[GameMonitor] 無法取得可發送頻道: {channel_id}")
                return

            await channel.send(text)
            logger.info(f"[GameMonitor] 已主動發送關心訊息到頻道 {channel_id}")
        except Exception as e:
            logger.error(f"[GameMonitor] 主動發送關心訊息失敗: {str(e)}")

    async def _trigger_llm_timeout_warning(self, game_name: str, duration_minutes: int):
        """
        超時警告 Prompt - 強制提醒
        
        當遊戲時長超過 120 分鐘時觸發
        """
        try:
            warning_prompt = f"""【系統指令】：User 已經玩『{game_name}』超過 {duration_minutes} 分鐘（共 {duration_minutes // 60} 小時 {duration_minutes % 60} 分）！

請以你的 Persona 語氣，發出一句具有關心（或調侃）的強制提醒：
- 提醒 User 要注意休息、保護眼睛、站起來活動一下
- 可以傲嬌地吃醋「早點玩完陪我」
- 可以溫柔地表示擔心
- 保持簡潔，一句話即可"""
            
            logger.warning(f"[GameMonitor] 超時警告 Prompt:\n{warning_prompt}")
            
            # TODO: 透過 context.llm_generate 或 Agent Tool Calling 發送此 Prompt
            
        except Exception as e:
            logger.error(f"[GameMonitor] 超時警告觸發失敗: {str(e)}")

    def _assemble_casual_prompt(self, game_name: str) -> str:
        """被動查詢時的 Casual Prompt"""
        variants = [
            f"Someone is currently playing {game_name}. Based on your personality, respond in one short sentence.",
            f"A person just mentioned they're playing {game_name}. Give a brief reaction in your voice.",
            f"The game {game_name} is being played. React in character with one comment.",
        ]
        return random.choice(variants)

    async def _call_llm_with_prompt(self, event: AstrMessageEvent, prompt: str) -> Optional[str]:
        """調用 LLM 生成回覆"""
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            
            if not provider_id:
                logger.warning("[GameMonitor] 無法獲取當前聊天模型 ID")
                return None
            
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            
            if not llm_resp:
                return None
            
            return llm_resp.completion_text
        
        except Exception as e:
            logger.error(f"[GameMonitor] LLM 調用失敗: {str(e)}")
            return None

    def _update_state_bot_status(self, status: str):
        """更新 bot_status"""
        try:
            state = self._read_game_state() or self._default_state()
            state["bot_status"] = status
            state["bot_status_updated"] = self._now_iso()
            state["last_bot_update"] = self._now_iso()

            self._persist_state(state)
        except Exception as e:
            logger.error(f"[GameMonitor] 更新 bot_status 失敗: {str(e)}")
