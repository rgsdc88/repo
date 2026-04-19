# ============================
# Ouroboros — Local Runtime launcher (FIXED FOR WINDOWS)
# ============================
import logging
import os, sys, json, time, uuid, pathlib, subprocess, datetime, threading, types, io, queue as _queue_mod
from typing import Any, Dict, List, Optional, Set, Tuple
from dotenv import load_dotenv

# Исправление кодировки для Windows (чтобы не было ошибки charmap)
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 1) Загрузка конфигов
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# 2) Функции настройки
def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = os.environ.get(name, default)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required secret: {name} in .env"
    return v

def get_cfg(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)

# 3) Чтение ключей
OPENROUTER_API_KEY = get_secret("OPENROUTER_API_KEY", required=True)
TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", required=True)
GITHUB_USER = get_cfg("GITHUB_USER")
GITHUB_REPO = get_cfg("GITHUB_REPO", default="ouroboros")
TOTAL_BUDGET_LIMIT = float(get_secret("TOTAL_BUDGET", default="10.0"))

# 4) Настройка путей
BASE_DIR = pathlib.Path(__file__).parent.resolve()
DRIVE_ROOT = (BASE_DIR / "storage").resolve()
REPO_DIR = BASE_DIR.resolve()

# 5) Константы
BRANCH_DEV = "ouroboros"
BRANCH_STABLE = "ouroboros-stable"
MAX_WORKERS = int(get_cfg("OUROBOROS_MAX_WORKERS", default="2"))
SOFT_TIMEOUT_SEC = 600
HARD_TIMEOUT_SEC = 1800
DIAG_HEARTBEAT_SEC = 30
DIAG_SLOW_CYCLE_SEC = 20

# 6) Импорты систем
sys.path.append(str(REPO_DIR))
from supervisor.state import (
    init as state_init, load_state, save_state, append_jsonl,
    update_budget_from_usage, status_text, rotate_chat_log_if_needed, init_state
)
from supervisor.telegram import (
    init as telegram_init, TelegramClient, send_with_budget, log_chat
)
from supervisor.git_ops import (
    init as git_ops_init, ensure_repo_present, safe_restart
)
from supervisor.queue import (
    enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
    persist_queue_snapshot, restore_pending_from_snapshot,
    cancel_task_by_id, queue_review_task, sort_pending
)
from supervisor.workers import (
    init as workers_init, get_event_q, WORKERS, PENDING, RUNNING,
    spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
    handle_chat_direct, _get_chat_agent, auto_resume_after_restart
)
from supervisor.events import dispatch_event
from ouroboros.consciousness import BackgroundConsciousness

# Вспомогательные функции для команд
def _handle_supervisor_command(text: str, chat_id: int, tg_offset: int = 0):
    lowered = text.strip().lower()
    if lowered.startswith("/panic"):
        kill_workers()
        raise SystemExit("PANIC")
    if lowered.startswith("/status"):
        status = status_text(WORKERS, PENDING, RUNNING, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC)
        send_with_budget(chat_id, status, force_budget=True)
        return "[Status sent]"
    return ""

def _get_owner_chat_id() -> Optional[int]:
    try:
        st = load_state()
        cid = st.get("owner_chat_id")
        return int(cid) if cid else None
    except: return None

# КРИТИЧЕСКИЙ БЛОК ДЛЯ WINDOWS
if __name__ == '__main__':
    # 7) Инициализация
    for sub in ["state", "logs", "memory", "index", "locks", "archive"]:
        (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)

    state_init(DRIVE_ROOT, TOTAL_BUDGET_LIMIT)
    init_state()

    REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
    git_ops_init(repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, remote_url=REMOTE_URL, branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE)

    TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))
    telegram_init(drive_root=DRIVE_ROOT, total_budget_limit=TOTAL_BUDGET_LIMIT, budget_report_every=10, tg_client=TG)

    workers_init(repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, max_workers=MAX_WORKERS, 
                 soft_timeout=SOFT_TIMEOUT_SEC, hard_timeout=HARD_TIMEOUT_SEC, 
                 total_budget_limit=TOTAL_BUDGET_LIMIT, branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE)

    _consciousness = BackgroundConsciousness(
        drive_root=DRIVE_ROOT, repo_dir=REPO_DIR, event_queue=get_event_q(), owner_chat_id_fn=_get_owner_chat_id
    )

    # 8) Запуск
    print(f"🚀 Ouroboros starting locally. Storage: {DRIVE_ROOT}")
    ensure_repo_present()
    kill_workers()
    spawn_workers(MAX_WORKERS)
    auto_resume_after_restart()
    _consciousness.start()

    # 9) Контекст событий
    _event_ctx = types.SimpleNamespace(
        DRIVE_ROOT=DRIVE_ROOT, REPO_DIR=REPO_DIR, BRANCH_DEV=BRANCH_DEV, BRANCH_STABLE=BRANCH_STABLE,
        TG=TG, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING, MAX_WORKERS=MAX_WORKERS,
        send_with_budget=send_with_budget, load_state=load_state, save_state=save_state,
        update_budget_from_usage=update_budget_from_usage, append_jsonl=append_jsonl,
        enqueue_task=enqueue_task, cancel_task_by_id=cancel_task_by_id, queue_review_task=queue_review_task,
        persist_queue_snapshot=persist_queue_snapshot, safe_restart=safe_restart,
        kill_workers=kill_workers, spawn_workers=spawn_workers, sort_pending=sort_pending,
        consciousness=_consciousness
    )

    # 10) MAIN LOOP
    offset = int(load_state().get("tg_offset") or 0)
    _last_message_ts = time.time()
    
    print("✅ System Ready. Waiting for Telegram messages...")

    while True:
        loop_started_ts = time.time()
        rotate_chat_log_if_needed(DRIVE_ROOT)
        ensure_workers_healthy()

        event_q = get_event_q()
        while True:
            try:
                evt = event_q.get_nowait()
                dispatch_event(evt, _event_ctx)
            except _queue_mod.Empty: 
                break

        enforce_task_timeouts()
        enqueue_evolution_task_if_needed()
        assign_tasks()
        
        # Telegram Polling
        _now = time.time()
        _active = (_now - _last_message_ts) < 300
        try:
            updates = TG.get_updates(offset=offset, timeout=(0 if _active else 10))
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                msg = upd.get("message") or {}
                if not msg: continue
                
                chat_id = msg["chat"]["id"]
                text = msg.get("text") or ""
                
                st = load_state()
                if st.get("owner_id") is None:
                    st["owner_id"] = msg["from"]["id"]
                    st["owner_chat_id"] = chat_id
                    save_state(st)
                    send_with_budget(chat_id, "✅ Owner registered.")
                    continue

                if text.startswith("/"):
                    _handle_supervisor_command(text, chat_id, offset)
                else:
                    agent = _get_chat_agent()
                    if not agent._busy:
                        threading.Thread(target=handle_chat_direct, args=(chat_id, text, None), daemon=True).start()
                        _last_message_ts = time.time()

        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(2)

        time.sleep(0.2)