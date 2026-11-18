"""Compatibility exports for the bridge, loaded only when requested.

Importing a transport or Monitor client must not initialize the retired broker
CLI, message lifecycle, or channel store.
"""

from importlib import import_module

_EXPORTS = {
    'AGY_CLI': ('._config', 'AGY_CLI'),
    'CLAUDE_CMD': ('._config', 'CLAUDE_CMD'),
    'CODEX_CLI': ('._config', 'CODEX_CLI'),
    'GEMINI_CLI': ('._config', 'GEMINI_CLI'),
    'GH_CHAR_LIMIT': ('._config', 'GH_CHAR_LIMIT'),
    '_MODEL_CACHE': ('._config', '_MODEL_CACHE'),
    '_MODEL_CACHE_TTL': ('._config', '_MODEL_CACHE_TTL'),
    '_detect_model_error': ('._model', '_detect_model_error'),
    '_extract_issue_number': ('._messaging', '_extract_issue_number'),
    '_format_review_chunk': ('._github', '_format_review_chunk'),
    '_gh_comment': ('._github', '_gh_comment'),
    '_git_status_snapshot': ('._broker', '_git_status_snapshot'),
    '_is_task_locked': ('._broker', '_is_task_locked'),
    '_post_review_to_github': ('._github', '_post_review_to_github'),
    '_remove_pid_file': ('._broker', '_remove_pid_file'),
    '_split_content': ('._github', '_split_content'),
    '_validate_file_writes': ('._broker', '_validate_file_writes'),
    '_write_pid_file': ('._broker', '_write_pid_file'),
    'acknowledge': ('._messaging', 'acknowledge'),
    'acknowledge_all': ('._messaging', 'acknowledge_all'),
    'ask_gemini': ('._gemini', 'ask_gemini'),
    'bridge_status': ('._broker', 'bridge_status'),
    'broker_cleanup': ('._broker', 'broker_cleanup'),
    'build_claude_prompt': ('._prompts', 'build_claude_prompt'),
    'build_codex_prompt': ('._prompts', 'build_codex_prompt'),
    'build_gemini_prompt': ('._prompts', 'build_gemini_prompt'),
    'check_inbox': ('._messaging', 'check_inbox'),
    'check_model': ('._model', 'check_model'),
    'default_gemini_model': ('._config', 'default_gemini_model'),
    'detect_sender': ('._messaging', 'detect_sender'),
    'get_conversation': ('._messaging', 'get_conversation'),
    'get_db': ('._db', 'get_db'),
    'get_session': ('._db', 'get_session'),
    'init_db': ('._db', 'init_db'),
    'interactive_mode': ('._cli', 'interactive_mode'),
    'main': ('._cli', 'main'),
    'process_all_claude': ('._cli', 'process_all_claude'),
    'process_all_codex': ('._cli', 'process_all_codex'),
    'process_all_gemini': ('._cli', 'process_all_gemini'),
    'process_and_respond': ('._gemini', 'process_and_respond'),
    'read_message': ('._messaging', 'read_message'),
    'send_message': ('._messaging', 'send_message'),
    'send_to_codex': ('._messaging', 'send_to_codex'),
    'send_to_gemini': ('._messaging', 'send_to_gemini'),
    'set_session': ('._db', 'set_session'),
}

def _load_module(name: str):
    match name:
        case '._broker':
            return import_module('._broker', __name__)
        case '._cli':
            return import_module('._cli', __name__)
        case '._config':
            return import_module('._config', __name__)
        case '._db':
            return import_module('._db', __name__)
        case '._gemini':
            return import_module('._gemini', __name__)
        case '._github':
            return import_module('._github', __name__)
        case '._messaging':
            return import_module('._messaging', __name__)
        case '._model':
            return import_module('._model', __name__)
        case '._prompts':
            return import_module('._prompts', __name__)
        case _:
            raise ImportError(f"unsupported bridge export module: {name}")


__all__ = sorted((*_EXPORTS, "DB_PATH", "GEMINI_DEFAULT_MODEL", "PID_DIR", "REPO_ROOT", "_PARENT_ENV"))


def __getattr__(name: str):
    if name == "GEMINI_DEFAULT_MODEL":
        return import_module("._config", __name__).default_gemini_model()
    if name in ("DB_PATH", "PID_DIR", "REPO_ROOT", "_PARENT_ENV"):
        return getattr(import_module("._config", __name__), name)
    if name in _EXPORTS:
        module, attribute = _EXPORTS[name]
        value = getattr(_load_module(module), attribute)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
