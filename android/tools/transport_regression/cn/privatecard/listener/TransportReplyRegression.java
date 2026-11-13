package cn.privatecard.listener;

import org.json.JSONObject;

/** Exercise the production Transport.Reply without network or an Android device. */
public final class TransportReplyRegression {
    private static int passed;
    private static JSONObject body(boolean ok, String result, String id) {
        return new JSONObject().put("ok", ok).put("result", result).put("event_id", id);
    }
    private static void check(String name, int status, JSONObject body, boolean expected) {
        boolean actual = new Transport.Reply(status, body).acknowledges("current-event-id");
        if (actual != expected) throw new AssertionError(name + ": expected " + expected + ", got " + actual);
        passed++; System.out.println("PASS " + name);
    }
    public static void main(String[] args) {
        check("delivered acknowledgement matches current event", 200, body(true, "delivered", "current-event-id"), true);
        check("review acknowledgement matches current event", 200, body(true, "review", "current-event-id"), true);
        check("duplicate acknowledgement matches current event", 200, body(true, "duplicate", "current-event-id"), true);
        check("different duplicate ID cannot retire current event", 200, body(true, "duplicate", "previous-event-id"), false);
        check("empty duplicate ID cannot retire current event", 200, body(true, "duplicate", ""), false);
        check("different delivered ID cannot retire current event", 200, body(true, "delivered", "previous-event-id"), false);
        check("different review ID cannot retire current event", 200, body(true, "review", "previous-event-id"), false);
        check("unsuccessful body cannot retire current event", 200, body(false, "delivered", "current-event-id"), false);
        check("unsuccessful HTTP status cannot retire current event", 503, body(true, "delivered", "current-event-id"), false);
        check("unknown result cannot retire current event", 200, body(true, "pending", "current-event-id"), false);
        check("missing body cannot retire current event", 200, null, false);
        System.out.println("{\"passed\":" + passed + "}");
    }
}
