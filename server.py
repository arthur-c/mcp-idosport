"""
MCP server for IDO Sport — read-only calendar and event plan.
Tools: get_calendar, get_event_plan. Credentials from env / .env.

Mode develop: IDO_DEV=1 ou --dev — connexion + rapatriement des events + dump JSON
des 10 derniers dans events_sample.json, puis sortie (pas de loop MCP).
"""

import asyncio
import json
import logging
import os
import sys

import anyio
from dotenv import load_dotenv
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ido_client import IdOClient, IdOClientError

# Load .env so IDO_USERNAME / IDO_PASSWORD are available
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ido-mcp")


def _get_events(
    start: str | None = None,
    end: str | None = None,
    event_types: list[str] | None = None,
    limit: int | None = None,
) -> str:
    """Blocking: load calendar events; returns JSON string."""
    client = IdOClient()
    data = client.get_events(start=start, end=end, event_types=event_types, limit=limit)
    return json.dumps(data, indent=2, ensure_ascii=False)


def _get_event_plan(event_id: str) -> str:
    """Blocking: load event plan; returns JSON string."""
    client = IdOClient()
    data = client.get_event_plan(event_id)
    return json.dumps(data, indent=2, ensure_ascii=False)


# Create server and register handlers with decorators (current MCP SDK API)
app = Server("ido-calendar")


@app.list_tools()
async def handle_list_tools(
    _req: types.ListToolsRequest,
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="get_calendar",
                description=(
                    "Récupère les séances prévues du calendrier IDO. "
                    "Types disponibles : swim, run, bike, race, autre, ppg. "
                    "Lecture seule."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "start": {
                            "type": "string",
                            "description": "Date de début (YYYY-MM-DD). Défaut : 1er janvier de l'année en cours.",
                        },
                        "end": {
                            "type": "string",
                            "description": "Date de fin (YYYY-MM-DD). Défaut : 31 décembre de l'année en cours.",
                        },
                        "types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filtrer par types de séance (ex. ['run', 'bike']). Si omis, tous les types sont renvoyés.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Nombre max d'événements à retourner (les plus récents). Si omis, tous.",
                        },
                    },
                },
            ),
            types.Tool(
                name="get_event_plan",
                description=(
                    "Récupère le détail et le plan d'un événement IDO à partir de son id (issu de get_calendar). "
                    "Retourne les intervalles structurés (échauffement, exercice, récup…), "
                    "les zones d'intensité (FC, allure), la description et le résumé par zone. "
                    "Lecture seule."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["event_id"],
                    "properties": {
                        "event_id": {
                            "type": "string",
                            "description": "Identifiant de l'événement (ex. 341345).",
                        },
                    },
                },
            ),
        ]
    )


@app.call_tool()
async def handle_call_tool(
    name: str,
    arguments: dict | None,
) -> types.CallToolResult | list[types.TextContent]:
    args = arguments or {}
    try:
        if name == "get_calendar":
            start = args.get("start")
            end = args.get("end")
            event_types = args.get("types")
            limit = args.get("limit")
            if limit is not None:
                limit = int(limit)
            text = await asyncio.to_thread(_get_events, start, end, event_types, limit)
            return [types.TextContent(type="text", text=text)]
        if name == "get_event_plan":
            event_id = args.get("event_id")
            if not event_id:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text="Error: event_id is required.")],
                    isError=True,
                )
            text = await asyncio.to_thread(_get_event_plan, str(event_id))
            return [types.TextContent(type="text", text=text)]
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
            isError=True,
        )
    except IdOClientError as e:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"IDO client error: {e}")],
            isError=True,
        )
    except Exception as e:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Error: {e}")],
            isError=True,
        )


EVENTS_SAMPLE_FILE = "events_sample.json"


def _startup_calendar_check(dev_mode: bool = False) -> None:
    """Run get_calendar at startup, log the last 10 events, dump their JSON.
    If dev_mode=True, exit after that (no MCP loop).
    """
    logger.info("Serveur MCP IDO démarré.")
    if dev_mode:
        logger.info("Mode develop: sortie après chargement des events.")
    # Vérifier que .env est chargé (login affiché, password masqué)
    _user = os.environ.get("IDO_USERNAME", "")
    _pwd = os.environ.get("IDO_PASSWORD", "")
    if _pwd:
        _masked = _pwd[:2] + "***" + _pwd[-1] if len(_pwd) > 4 else "****"
    else:
        _masked = "(vide)"
    logger.info("Credentials: IDO_USERNAME=%r, IDO_PASSWORD=%s", _user or "(vide)", _masked)
    logger.info("Vérification connexion IDO (get_calendar)...")
    try:
        raw = _get_events()
        data = json.loads(raw)
    except IdOClientError as e:
        logger.warning("Connexion IDO échouée au démarrage: %s", e)
        return
    except Exception as e:
        logger.warning("Erreur au chargement du calendrier: %s", e)
        return

    # Extraire la liste d'événements (structure possible: liste ou dict avec clé events/items)
    events = None
    if isinstance(data, list):
        events = data
    elif isinstance(data, dict):
        events = data.get("events") or data.get("items") or data.get("data")
        if events is None and data:
            for v in data.values():
                if isinstance(v, list) and v:
                    events = v
                    break

    if not events or not isinstance(events, list):
        logger.info("Connexion IDO OK. Aucun événement à afficher (ou format inattendu).")
        if dev_mode:
            return
        anyio.run(_run_server)
        return

    logger.info("Connexion IDO OK. %d événement(s) chargé(s).", len(events))

    # En mode dev : afficher les types distincts et classNames pour debug
    if dev_mode:
        from collections import Counter
        type_counts = Counter(
            ev.get("type", "(no type)") if isinstance(ev, dict) else "(not a dict)"
            for ev in events
        )
        logger.info("Types d'événements : %s", dict(type_counts))
        class_counts = Counter(
            ev.get("className", "(no class)") if isinstance(ev, dict) else "(n/a)"
            for ev in events
        )
        logger.info("ClassNames distincts : %s", dict(class_counts))

    last_n = events[-10:] if len(events) >= 10 else events
    logger.info("Derniers %d événement(s) :", len(last_n))
    for i, ev in enumerate(last_n, 1):
        if isinstance(ev, dict):
            eid = ev.get("id") or ev.get("caleventId") or ev.get("refId") or "?"
            title = ev.get("title") or ev.get("name") or ev.get("label") or "-"
            start = ev.get("start") or ev.get("date") or ev.get("startDate") or "-"
            logger.info("  %d. id=%s | %s | %s", i, eid, title, start)
        else:
            logger.info("  %d. %s", i, ev)

    # Dump JSON uniquement en mode dev
    if dev_mode:
        try:
            with open(EVENTS_SAMPLE_FILE, "w", encoding="utf-8") as f:
                json.dump(last_n, f, indent=2, ensure_ascii=False)
            logger.info("Dump JSON des %d derniers events → %s", len(last_n), EVENTS_SAMPLE_FILE)
        except OSError as e:
            logger.warning("Impossible d'écrire %s: %s", EVENTS_SAMPLE_FILE, e)

    if dev_mode:
        logger.info("Mode develop: fin (pas de loop MCP).")
        return

    anyio.run(_run_server)


async def _run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main() -> int:
    dev_mode = os.environ.get("IDO_DEV", "").strip().lower() in ("1", "true", "yes") or "--dev" in sys.argv
    _startup_calendar_check(dev_mode=dev_mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
