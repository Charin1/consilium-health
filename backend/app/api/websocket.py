"""
WebSocket API - Real-time mission updates.
"""
from __future__ import annotations

import logging
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.workflow_contracts import build_event

logger = logging.getLogger("consilium.websocket")
router = APIRouter()


class ConnectionManager:
    """Manages mission-scoped websocket connections."""

    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, mission_id: str) -> None:
        """Accept and register a connection."""
        await websocket.accept()
        self.active_connections.setdefault(mission_id, set()).add(websocket)
        logger.info(
            "WS connected: mission=%s total=%d",
            mission_id[:8],
            len(self.active_connections[mission_id]),
        )

    def disconnect(self, websocket: WebSocket, mission_id: str) -> None:
        """Remove a connection."""
        if mission_id in self.active_connections:
            self.active_connections[mission_id].discard(websocket)
            if not self.active_connections[mission_id]:
                del self.active_connections[mission_id]
        logger.info("WS disconnected: mission=%s", mission_id[:8])

    async def broadcast_to_mission(self, mission_id: str, message: dict) -> None:
        """Broadcast message to all connections for a mission."""
        connections = self.active_connections.get(mission_id)
        if not connections:
            return

        stale_connections = []
        for connection in list(connections):
            try:
                await connection.send_json(message)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("WS send failed for mission=%s: %s", mission_id[:8], exc)
                stale_connections.append(connection)

        for connection in stale_connections:
            self.disconnect(connection, mission_id)


manager = ConnectionManager()


@router.websocket("/mission/{mission_id}")
async def websocket_endpoint(websocket: WebSocket, mission_id: str) -> None:
    """
    WebSocket endpoint for real-time mission updates.

    Event envelope:
    - type
    - schema_version
    - mission_id
    - timestamp
    - payload
    """
    await manager.connect(websocket, mission_id)

    try:
        await websocket.send_json(
            build_event(mission_id, "connected", payload={"mission_id": mission_id}, source="websocket")
        )

        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")

            if message_type == "ping":
                await websocket.send_json(
                    build_event(mission_id, "pong", payload={"ok": True}, source="websocket")
                )
    except WebSocketDisconnect:
        manager.disconnect(websocket, mission_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("WS receive loop terminated for mission=%s: %s", mission_id[:8], exc)
        manager.disconnect(websocket, mission_id)


# Export manager for use by orchestrator

def get_connection_manager() -> ConnectionManager:
    return manager
