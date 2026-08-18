// Miroir JavaScript de `protocol.py`, `events.py` et `core/capabilities.py`.
//
// **La source de vérité reste le Python.** Ce fichier ne fait que redire les
// mêmes constantes pour le navigateur, qui ne peut pas importer un module
// Python. C'est de la duplication assumée, et la duplication assumée se
// désynchronise en silence : `tests/test_protocol.py` compare les deux et
// échoue dès qu'un nom ou une valeur diverge. Ne pas modifier ce fichier sans
// modifier son homologue Python — le test le rappellera.
//
// Le format d'écriture (`NOM: "valeur",` sur une ligne, blocs `Object.freeze`)
// est celui que ce test sait lire. Le respecter.

export const PROTOCOL_VERSION = 1;

/** Ce qu'un client envoie : des intentions, jamais des décisions. */
export const ClientMessage = Object.freeze({
  HELLO: "hello",
  PROMPT_SEND: "prompt.send",
  STREAM_STOP: "stream.stop",
  FLOOR_REQUEST: "floor.request",
  FLOOR_RELEASE: "floor.release",
  FLOOR_PREEMPT: "floor.preempt",
  TOOL_APPROVE: "tool.approve",
  PING: "ping",
});

/** Trames propres au protocole. Les autres portent un type d'événement métier. */
export const ServerMessage = Object.freeze({
  SNAPSHOT: "snapshot",
  QUEUED: "queued",
  PRESENCE: "presence",
  AGENT: "agent",
  ERROR: "error",
  PONG: "pong",
});

/** Faits observables dans un salon. */
export const EventType = Object.freeze({
  SESSION_READY: "session.ready",
  SESSION_ERROR: "session.error",
  TURN_STARTED: "turn.started",
  TURN_ENDED: "turn.ended",
  ASSISTANT_DELTA: "assistant.delta",
  ASSISTANT_MESSAGE: "assistant.message",
  THINKING_STARTED: "thinking.started",
  TOOL_USE: "tool.use",
  TOOL_RESULT: "tool.result",
  TOOL_APPROVAL_REQUESTED: "tool.approval_requested",
  TOOL_APPROVAL_RESOLVED: "tool.approval_resolved",
  FLOOR_CHANGED: "floor.changed",
  MEMBER_JOINED: "member.joined",
  RATE_LIMIT: "rate_limit",
});

/**
 * Droits. Côté client ils ne servent qu'à **griser des boutons** : le serveur
 * revérifie chaque intention. Un bouton actif de force ne donne rien.
 */
export const Capability = Object.freeze({
  READ: "room.read",
  PROPOSE: "room.propose",
  SPEAK: "room.speak",
  TOOLS_APPROVE: "room.tools.approve",
  PREEMPT: "room.preempt",
  STOP: "room.stop",
  INVITE: "room.invite",
  MEMBERS_MANAGE: "room.members.manage",
  ROLES_MANAGE: "room.roles.manage",
  SETTINGS: "room.settings",
  DELETE: "room.delete",
});

/** Construit une trame sortante. Le serveur ignore `room_id` et `ts` entrants. */
export function frame(type, data = {}) {
  return { v: PROTOCOL_VERSION, type, data };
}
