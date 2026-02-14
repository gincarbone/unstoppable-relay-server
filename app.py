import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("relay")

app = FastAPI(title="Unstoppable Relay", version="1.0.0")


@dataclass
class PeerSession:
    peer_id: str
    ws: WebSocket
    display_name: str = ""
    public_key_x509: str = ""
    stun_ip: str = ""
    stun_port: int = 0
    udp_port: int = 0
    last_seen: float = field(default_factory=lambda: time.time())

    def has_endpoint(self) -> bool:
        return bool(self.stun_ip and self.stun_port > 0)


peers: Dict[str, PeerSession] = {}
peers_lock = asyncio.Lock()


async def send_json(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_text(json.dumps(payload, separators=(",", ":")))
    except Exception:
        # Connection may already be closed; caller handles cleanup.
        pass


async def broadcast_announce(source_peer_id: str, source: PeerSession) -> None:
    msg = {
        "type": "announce",
        "peerId": source_peer_id,
        "displayName": source.display_name,
        "publicKeyX509": source.public_key_x509,
        "timestamp": int(time.time() * 1000),
    }
    async with peers_lock:
        targets = [p.ws for pid, p in peers.items() if pid != source_peer_id]
    await asyncio.gather(*(send_json(ws, msg) for ws in targets), return_exceptions=True)


async def send_punch_pair(a: PeerSession, b: PeerSession) -> None:
    # Message format expected by Android SignalingClient.handleMessage(...)
    to_a = {
        "type": "punch_now",
        "peerId": b.peer_id,
        "stunIp": b.stun_ip,
        "stunPort": b.stun_port,
        "udpPort": b.udp_port,
    }
    to_b = {
        "type": "punch_now",
        "peerId": a.peer_id,
        "stunIp": a.stun_ip,
        "stunPort": a.stun_port,
        "udpPort": a.udp_port,
    }
    await asyncio.gather(
        send_json(a.ws, to_a),
        send_json(b.ws, to_b),
        return_exceptions=True,
    )


async def try_pair_with_registered_peers(peer_id: str) -> None:
    async with peers_lock:
        source = peers.get(peer_id)
        if source is None or not source.has_endpoint():
            return
        others = [p for pid, p in peers.items() if pid != peer_id and p.has_endpoint()]
    await asyncio.gather(*(send_punch_pair(source, other) for other in others), return_exceptions=True)


async def upsert_peer(ws: WebSocket, peer_id: str) -> PeerSession:
    async with peers_lock:
        existing = peers.get(peer_id)
        if existing:
            existing.ws = ws
            existing.last_seen = time.time()
            return existing
        created = PeerSession(peer_id=peer_id, ws=ws)
        peers[peer_id] = created
        return created


async def cleanup_peer(peer_id: Optional[str], ws: WebSocket) -> None:
    if not peer_id:
        return
    async with peers_lock:
        session = peers.get(peer_id)
        if session and session.ws is ws:
            del peers[peer_id]
            logger.info("peer disconnected: %s", peer_id)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    async with peers_lock:
        connected = len(peers)
    return JSONResponse({"ok": True, "connectedPeers": connected})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    current_peer_id: Optional[str] = None
    logger.info("websocket connected")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            peer_id = msg.get("peerId", "")
            if peer_id:
                current_peer_id = peer_id

            if msg_type == "announce" and peer_id:
                session = await upsert_peer(ws, peer_id)
                session.display_name = str(msg.get("displayName", ""))[:120]
                session.public_key_x509 = str(msg.get("publicKeyX509", ""))[:8192]
                session.last_seen = time.time()
                logger.info("announce peer=%s name=%s", peer_id, session.display_name)
                await broadcast_announce(peer_id, session)

            elif msg_type == "punch_register" and peer_id:
                session = await upsert_peer(ws, peer_id)
                session.stun_ip = str(msg.get("stunIp", ""))[:64]
                session.stun_port = int(msg.get("stunPort", 0) or 0)
                session.udp_port = int(msg.get("udpPort", 0) or 0)
                session.last_seen = time.time()
                logger.info(
                    "punch_register peer=%s endpoint=%s:%s udp=%s",
                    peer_id,
                    session.stun_ip,
                    session.stun_port,
                    session.udp_port,
                )
                await try_pair_with_registered_peers(peer_id)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("ws error: %s", exc)
    finally:
        await cleanup_peer(current_peer_id, ws)

