import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("relay")

app = FastAPI(title="Unstoppable Relay (WebRTC)", version="2.0.0")


@dataclass
class PeerSession:
    peer_id: str
    ws: WebSocket
    display_name: str = ""
    public_key_x509: str = ""
    last_seen: float = field(default_factory=lambda: time.time())


peers: Dict[str, PeerSession] = {}
peers_lock = asyncio.Lock()


async def send_json(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_text(json.dumps(payload, separators=(",", ":")))
    except Exception:
        pass


async def broadcast_announce(source_peer_id: str, source: PeerSession) -> None:
    """Broadcast peer announcement to all other peers."""
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


async def route_to_peer(to_peer_id: str, message: dict) -> bool:
    """Route a message to a specific peer. Returns True if delivered."""
    async with peers_lock:
        target = peers.get(to_peer_id)
    if target:
        await send_json(target.ws, message)
        return True
    return False


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
            peer_id = msg.get("peerId", "") or msg.get("fromPeerId", "")
            to_peer_id = msg.get("toPeerId", "")

            if peer_id:
                current_peer_id = peer_id

            # Handle peer announcement
            if msg_type == "announce" and peer_id:
                # Collect existing peers BEFORE registering, so we can send them back
                async with peers_lock:
                    existing_peers = [
                        {"peerId": pid, "displayName": p.display_name}
                        for pid, p in peers.items()
                        if pid != peer_id
                    ]

                session = await upsert_peer(ws, peer_id)
                session.display_name = str(msg.get("displayName", ""))[:120]
                session.public_key_x509 = str(msg.get("publicKeyX509", ""))[:8192]
                session.last_seen = time.time()
                logger.info("announce peer=%s name=%s existing=%d", peer_id, session.display_name, len(existing_peers))

                # Broadcast this peer to all others
                await broadcast_announce(peer_id, session)

                # Send already-connected peers back to the announcing peer
                # with "youInitiate: true" so this peer knows to send the offer
                for ep in existing_peers:
                    await send_json(ws, {
                        "type": "announce",
                        "peerId": ep["peerId"],
                        "displayName": ep["displayName"],
                        "timestamp": int(time.time() * 1000),
                        "youInitiate": True,
                    })

            # Route SDP offer to target peer
            elif msg_type == "sdp_offer" and to_peer_id:
                await upsert_peer(ws, peer_id)
                delivered = await route_to_peer(to_peer_id, msg)
                logger.info(
                    "sdp_offer from=%s to=%s delivered=%s",
                    peer_id, to_peer_id, delivered
                )

            # Route SDP answer to target peer
            elif msg_type == "sdp_answer" and to_peer_id:
                await upsert_peer(ws, peer_id)
                delivered = await route_to_peer(to_peer_id, msg)
                logger.info(
                    "sdp_answer from=%s to=%s delivered=%s",
                    peer_id, to_peer_id, delivered
                )

            # Route ICE candidate to target peer
            elif msg_type == "ice_candidate" and to_peer_id:
                await upsert_peer(ws, peer_id)
                delivered = await route_to_peer(to_peer_id, msg)
                logger.debug(
                    "ice_candidate from=%s to=%s delivered=%s",
                    peer_id, to_peer_id, delivered
                )

            # Route relay data (gossip messages when WebRTC unavailable)
            elif msg_type == "relay_data" and to_peer_id:
                await upsert_peer(ws, peer_id)
                delivered = await route_to_peer(to_peer_id, msg)
                logger.debug(
                    "relay_data from=%s to=%s delivered=%s size=%d",
                    peer_id, to_peer_id, delivered, len(raw),
                )

            # Broadcast relay data to all peers (gossip protocol)
            elif msg_type == "relay_broadcast" and peer_id:
                await upsert_peer(ws, peer_id)
                relay_msg = {
                    "type": "relay_data",
                    "fromPeerId": peer_id,
                    "toPeerId": "",
                    "payload": msg.get("payload", ""),
                }
                async with peers_lock:
                    targets = [
                        p.ws for pid, p in peers.items() if pid != peer_id
                    ]
                await asyncio.gather(
                    *(send_json(t, relay_msg) for t in targets),
                    return_exceptions=True,
                )
                logger.debug(
                    "relay_broadcast from=%s to=%d peers",
                    peer_id, len(targets),
                )

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("ws error: %s", exc)
    finally:
        await cleanup_peer(current_peer_id, ws)
