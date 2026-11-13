package cn.privatecard.listener;

import android.content.Context;
import android.os.SystemClock;
import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

final class Config {
    static boolean enabled;
    static Snapshot current;
    static String latestStatus;
    Config(Context context) {}
    boolean configured() { return enabled; }
    Snapshot snapshot() { return current; }
    void status(String message) { latestStatus = message; }
    static final class Snapshot {
        final String url, token, deviceId;
        Snapshot(String url, String token, String deviceId) {
            this.url = url; this.token = token; this.deviceId = deviceId;
        }
    }
}

final class ReceiptListenerService { static volatile boolean connected = true; }

final class Outbox {
    static final Outbox INSTANCE = new Outbox();
    final Map<String, Entry> pending = new LinkedHashMap<>();
    final List<String> delivered = new ArrayList<>();
    final Map<String, Integer> failures = new LinkedHashMap<>();
    static Outbox get(Context context) { return INSTANCE; }
    void clear() { pending.clear(); delivered.clear(); failures.clear(); }
    void add(String id) { pending.put(id, new Entry(id, "body:" + id, Config.current.url)); }
    List<Entry> due(String origin) {
        List<Entry> entries = new ArrayList<>();
        for (Entry entry : pending.values()) {
            if (entry.origin.equals(origin)) entries.add(entry);
            if (entries.size() == 20) break;
        }
        return entries;
    }
    void delivered(String id) {
        if (pending.remove(id) == null) throw new AssertionError("Acknowledged an absent/duplicate event: " + id);
        delivered.add(id);
    }
    void failed(Entry entry, boolean permanent) {
        if (!pending.containsKey(entry.id)) throw new AssertionError("Failed event was already removed");
        failures.put(entry.id, failures.getOrDefault(entry.id, 0) + 1);
    }
    static final class Entry {
        final String id, body, origin;
        Entry(String id, String body, String origin) { this.id = id; this.body = body; this.origin = origin; }
    }
}

final class Transport {
    static long delay;
    static int heartbeats, events, staleEvents, rejectHeartbeatAt, throwHeartbeatAt, throwEventAt, badAckAt;
    static Runnable afterEvent;
    static final List<Call> calls = new ArrayList<>();
    static final List<Long> successfulHeartbeats = new ArrayList<>();
    static final Map<String, Long> serverSeen = new LinkedHashMap<>();
    static void reset() {
        delay = 0; heartbeats = events = staleEvents = rejectHeartbeatAt = throwHeartbeatAt = throwEventAt = badAckAt = 0;
        calls.clear(); successfulHeartbeats.clear(); serverSeen.clear(); afterEvent = null;
    }
    static Reply post(Config.Snapshot snapshot, String path, String body) throws Exception {
        calls.add(new Call(snapshot, path, body));
        SystemClock.time += delay;
        if (path.endsWith("heartbeat")) {
            heartbeats++;
            if (heartbeats == throwHeartbeatAt) throw new IOException("simulated heartbeat disconnect");
            if (heartbeats == rejectHeartbeatAt) return new Reply(503, false, "");
            successfulHeartbeats.add(SystemClock.time);
            serverSeen.put(snapshot.url, SystemClock.time);
            return new Reply(200, true, "");
        }
        events++;
        if (!serverSeen.containsKey(snapshot.url) || SystemClock.time - serverSeen.get(snapshot.url) > 120000L) staleEvents++;
        if (events == throwEventAt) throw new IOException("simulated event disconnect");
        if (afterEvent != null) afterEvent.run();
        return new Reply(200, true, events == badAckAt ? "wrong-id" : body.substring("body:".length()));
    }
    static final class Reply {
        final int status; final boolean success; final String id;
        Reply(int status, boolean success, String id) { this.status = status; this.success = success; this.id = id; }
        boolean ok() { return success; }
        boolean acknowledges(String expected) { return success && id.equals(expected); }
    }
    static final class Call {
        final Config.Snapshot config; final String path, body;
        Call(Config.Snapshot config, String path, String body) { this.config = config; this.path = path; this.body = body; }
    }
}
