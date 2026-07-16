"""The Gradio dashboard: browse, translate, search, publish.

Thin UI over `service.py`. All data access, LLM calls and Discord sends live
there; this file only wires widgets to those functions and manages a little
selection state. Gradio is imported lazily by `main()` so the core package has
no hard dependency on it.
"""

from __future__ import annotations

import argparse
import os

import gradio as gr

from . import service as svc

_STATUS_CHOICES = ["all", "new", "notified", "rejected"]

_CSS = """
#detail_card { max-height: 70vh; overflow-y: auto; padding-right: 6px; }
.gradio-container { max-width: 1400px !important; }
footer { display: none !important; }
"""


def _status_arg(value: str) -> str | None:
    return None if value == "all" else value


def _source_arg(value: str) -> str | None:
    return None if value in ("", "all") else value


def _refresh(status, min_score, source, text, show_hidden):
    rows, ids = svc.list_rows(
        status=_status_arg(status),
        min_score=int(min_score) if min_score else None,
        source=_source_arg(source),
        text=text,
        show_hidden=show_hidden,
    )
    return rows, ids, svc.stats_line()


def build() -> gr.Blocks:
    with gr.Blocks(title="Job Dashboard") as demo:
        gr.Markdown("# 🎓 Job Dashboard")
        stats_md = gr.Markdown(svc.stats_line())

        ids_state = gr.State([])
        selected_id = gr.State(None)

        # --- Search (fetch) ------------------------------------------------
        with gr.Accordion("🔎 Search new jobs (fetches from all sources)", open=False):
            gr.Markdown(
                "Type keywords, pick topics, or upload a resume. This runs the full "
                "pipeline (**all sources, including the paid Apify one**) and **stores** "
                "results — it sends nothing to Discord. Review and publish below."
            )
            with gr.Row():
                kw_box = gr.Textbox(
                    label="Keywords (comma-separated)",
                    placeholder="reinforcement learning, computer vision",
                    scale=3,
                )
                # allow_custom_value: the whole point of dynamic search is that
                # resume extraction and the user can add topics/locations that
                # aren't in the preset list. Without it, a Dropdown rejects any
                # value outside `choices` and raises on the next interaction.
                topics_dd = gr.Dropdown(
                    label="Topics",
                    choices=svc.topic_choices(),
                    multiselect=True,
                    allow_custom_value=True,
                    scale=2,
                )
                loc_dd = gr.Dropdown(
                    label="Locations",
                    choices=svc.location_choices(),
                    multiselect=True,
                    allow_custom_value=True,
                    scale=2,
                )
            with gr.Row():
                resume_file = gr.File(label="Resume (PDF)", file_types=[".pdf"], type="filepath")
                extract_btn = gr.Button("Extract keywords from resume")
            gr.Markdown(
                "_Uploading a resume and extracting sends its text to the configured LLM "
                "endpoint (a public tunnel). Don't upload anything you wouldn't send there._",
                elem_id="resume_warn",
            )
            resume_status = gr.Markdown("")

            prepare_btn = gr.Button("🔎 Prepare search", variant="primary")
            queries_box = gr.Textbox(label="Queries to run", lines=3, interactive=False)
            scope_md = gr.Markdown("")
            run_btn = gr.Button("▶ Confirm & run search", variant="stop", visible=False)
            search_status = gr.Markdown("")

        # --- Filters -------------------------------------------------------
        with gr.Row():
            status_radio = gr.Radio(_STATUS_CHOICES, value="all", label="Status", scale=2)
            min_score = gr.Slider(0, 100, value=0, step=5, label="Min score", scale=2)
            source_dd = gr.Dropdown(
                choices=["all", *svc.source_choices()], value="all", label="Source", scale=2
            )
            text_box = gr.Textbox(
                label="Search text", placeholder="title / org / location", scale=2
            )
            show_hidden = gr.Checkbox(label="Show hidden", value=False, scale=1)

        # --- Master / detail ----------------------------------------------
        with gr.Row():
            with gr.Column(scale=3):
                table = gr.Dataframe(
                    headers=svc.ROW_HEADERS,
                    datatype=["number", "str", "str", "str", "str", "str", "str", "str"],
                    interactive=False,
                    wrap=True,
                    label="Jobs",
                )
            with gr.Column(scale=2):
                detail = gr.HTML("<p>Select a job to see details.</p>", elem_id="detail_card")
                confirm_chk = gr.Checkbox(label="", visible=False, value=False)
                with gr.Row():
                    publish_btn = gr.Button("📢 Publish to Discord", variant="primary")
                    hide_btn = gr.Button("🙈 Hide")
                action_status = gr.Markdown("")

        filters = [status_radio, min_score, source_dd, text_box, show_hidden]

        # --- Wiring --------------------------------------------------------
        def do_refresh(*f):
            return _refresh(*f)

        for f in filters:
            f.change(do_refresh, filters, [table, ids_state, stats_md])

        demo.load(do_refresh, filters, [table, ids_state, stats_md])

        # Row selection -> detail
        def on_select(ids, evt: gr.SelectData):
            if evt.index is None:
                return None, gr.update(), gr.update(visible=False), ""
            row = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
            if row is None or row >= len(ids):
                return None, "<p>Selection out of range.</p>", gr.update(visible=False), ""
            jid = ids[row]
            d = svc.job_detail(jid)
            return (
                jid,
                d.html,
                gr.update(visible=d.needs_confirm, label=d.confirm_label, value=False),
                "",
            )

        table.select(
            on_select, [ids_state], [selected_id, detail, confirm_chk, action_status]
        )

        # Resume extraction
        def do_extract(path):
            kws, topics, status = svc.prefill_from_resume(path)
            return kws, topics, status

        extract_btn.click(do_extract, [resume_file], [kw_box, topics_dd, resume_status])

        # Prepare search -> preview + reveal confirm
        def do_prepare(kw, topics):
            queries, scope = svc.preview_search(kw, topics)
            return queries, scope, gr.update(visible=True)

        prepare_btn.click(do_prepare, [kw_box, topics_dd], [queries_box, scope_md, run_btn])

        # Confirm & run (disable while running, then refresh + re-enable)
        def do_run(kw, topics, locs, *f):
            status = svc.run_search(kw, topics, locs)
            rows, ids, stats = _refresh(*f)
            return status, rows, ids, stats

        run_btn.click(
            lambda: (gr.update(interactive=False, value="⏳ Running…"), "Searching…"),
            None,
            [run_btn, search_status],
        ).then(
            do_run,
            [kw_box, topics_dd, loc_dd, *filters],
            [search_status, table, ids_state, stats_md],
        ).then(
            lambda: gr.update(interactive=True, value="▶ Confirm & run search"),
            None,
            [run_btn],
        )

        # Publish
        def do_publish(jid, confirm, *f):
            status = svc.publish_job(jid, confirm)
            rows, ids, stats = _refresh(*f)
            return status, rows, ids, stats

        publish_btn.click(
            do_publish,
            [selected_id, confirm_chk, *filters],
            [action_status, table, ids_state, stats_md],
        )

        # Hide
        def do_hide(jid, *f):
            status = svc.hide_job(jid)
            rows, ids, stats = _refresh(*f)
            return status, rows, ids, stats

        hide_btn.click(
            do_hide, [selected_id, *filters], [action_status, table, ids_state, stats_md]
        )

    return demo


def _parse_auth(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    user, sep, pw = value.partition(":")
    if not sep or not user or not pw:
        raise SystemExit("--auth / GRADIO_AUTH must be 'username:password'")
    return (user, pw)


def wait_for_llm(interval: float = 10.0, timeout: float = 0.0) -> bool:
    """Block until the self-hosted LLM endpoint is reachable.

    This is the container's startup gate: on `docker compose up` the Colab
    notebook is usually not running yet, so rather than launch a dashboard whose
    translation is dead, we poll and tell the user what to do.

    `settings.yaml` is re-read on every poll on purpose — the tunnel URL changes
    each Colab session, so the user can edit `llm.colab_base_url` in the mounted
    config and this picks it up without a restart. `timeout=0` waits forever.
    Returns True when the endpoint came online, False if it timed out.
    """
    import asyncio
    import time

    from ..config import ConfigError, Secrets, load_settings
    from ..llm.assist import endpoint_online

    start = time.monotonic()
    last_announced: str | None = None

    while True:
        base = ""
        settings = secrets = None
        try:
            secrets = Secrets()
            settings = load_settings(secrets=secrets)
            base = settings.llm.colab_base_url.strip()
        except ConfigError as exc:
            if last_announced != "config":
                print(f"⏳ Waiting: configuration not ready — {exc}", flush=True)
                last_announced = "config"

        if base and settings and asyncio.run(endpoint_online(settings.llm, secrets)):
            print(f"✅ LLM endpoint is online: {base}", flush=True)
            return True

        if last_announced != base:
            if base:
                print(
                    f"⏳ Waiting for the LLM endpoint at {base}\n"
                    "   → Start your Colab notebook / vLLM server so it becomes reachable.\n"
                    "   → If the tunnel URL changed, edit llm.colab_base_url in "
                    "config/settings.yaml — this picks it up automatically.",
                    flush=True,
                )
            elif settings is not None:
                print(
                    "⏳ No LLM endpoint configured.\n"
                    "   → Set llm.colab_base_url in config/settings.yaml to your "
                    "Colab/vLLM URL. Waiting for it to appear...",
                    flush=True,
                )
            last_announced = base

        if timeout > 0 and (time.monotonic() - start) > timeout:
            print(
                "⚠️  Timed out waiting for the LLM endpoint; starting the dashboard anyway. "
                "German translation will be unavailable until it comes online.",
                flush=True,
            )
            return False

        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m job_alerts dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true", help="expose a public Gradio link")
    parser.add_argument(
        "--auth", default=os.environ.get("GRADIO_AUTH"), help="username:password for login"
    )
    parser.add_argument(
        "--wait-for-llm",
        action="store_true",
        help="poll the LLM endpoint and wait until it is online before launching",
    )
    parser.add_argument("--wait-interval", type=float, default=10.0, help="seconds between polls")
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=0.0,
        help="give up waiting after this many seconds (0 = wait forever)",
    )
    args = parser.parse_args(argv)

    auth = _parse_auth(args.auth)
    if args.share and auth is None:
        raise SystemExit(
            "--share exposes Discord publishing and paid search to the public internet.\n"
            "Refusing to launch without auth. Pass --auth user:pass (or set GRADIO_AUTH)."
        )

    if args.wait_for_llm:
        wait_for_llm(interval=args.wait_interval, timeout=args.wait_timeout)

    demo = build()
    demo.launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        auth=auth,
        theme=gr.themes.Soft(),
        css=_CSS,
    )
    return 0
