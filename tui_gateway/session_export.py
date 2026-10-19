"""Profile-scoped sanitized /save output for TUI, Desktop and compute-host sessions."""


def write_save_snapshot(session: dict, data: dict, path) -> None:
    from tui_gateway import server
    from hermes_cli.session_export import render_session_for_save

    path.parent.mkdir(parents=True, exist_ok=True)
    with server._session_profile_runtime_scope(session, hydrate_secrets=False):
        path.write_text(render_session_for_save(data, "json"), encoding="utf-8")
