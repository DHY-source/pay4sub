package cn.privatecard.listener;

import android.content.Context;
import android.os.SystemClock;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** Tests the production UploadWorker class, with only its dependencies replaced. */
public final class UploadWorkerRegression {
    private static int passed;
    private static long slowGap;
    private static int slowHeartbeats;
    private static UploadWorker worker;
    private static final Outbox outbox = Outbox.INSTANCE;
    private interface Test { void run() throws Exception; }
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
    private static void reset() throws Exception {
        Config.enabled = false;
        Config.current = new Config.Snapshot("https://old.example", "token-old", "phone");
        Config.latestStatus = "";
        ReceiptListenerService.connected = true;
        SystemClock.time = 0;
        Transport.reset(); outbox.clear();
        Constructor<UploadWorker> constructor = UploadWorker.class.getDeclaredConstructor(Context.class);
        constructor.setAccessible(true);
        worker = constructor.newInstance(new Context());
        Field field = UploadWorker.class.getDeclaredField("executor");
        field.setAccessible(true);
        ScheduledExecutorService executor = (ScheduledExecutorService) field.get(worker);
        executor.shutdownNow();
        check(executor.awaitTermination(5, TimeUnit.SECONDS), "Background test executor did not terminate");
        Config.enabled = true;
    }
    private static void tick() throws Exception {
        Method method = UploadWorker.class.getDeclaredMethod("tick");
        method.setAccessible(true); method.invoke(worker);
    }
    private static void add(int count) {
        for (int i = 1; i <= count; i++) outbox.add("event-" + i);
    }
    private static void test(String name, Test test) throws Exception {
        reset(); test.run(); passed++; System.out.println("PASS " + name);
    }
    public static void main(String[] args) throws Exception {
        test("slow twenty-event batch keeps heartbeats fresh", () -> {
            add(20); Transport.delay = 7000; tick();
            check(outbox.delivered.size() == 20 && outbox.pending.isEmpty(), "Did not acknowledge all twenty events");
            check(Transport.staleEvents == 0, "Uploads observed a stale server heartbeat");
            check(Transport.heartbeats > 1, "Heartbeats were starved by the batch");
            long previous = 0;
            for (long heartbeat : Transport.successfulHeartbeats) {
                slowGap = Math.max(slowGap, heartbeat - previous); previous = heartbeat;
            }
            check(slowGap < 120000L, "Heartbeat gap exceeded the server's offline limit");
            slowHeartbeats = Transport.heartbeats;
        });
        test("quick batch avoids redundant heartbeats", () -> {
            add(20); Transport.delay = 1000; tick();
            check(Transport.heartbeats == 1 && Transport.events == 20, "Fast batch sent redundant requests");
            SystemClock.time += 15000; tick();
            check(Transport.heartbeats == 1 && Transport.events == 20, "Already acknowledged events were uploaded again");
        });
        test("rejected initial heartbeat retains every event and retries", () -> {
            add(2); Transport.rejectHeartbeatAt = 1; tick();
            check(Transport.events == 0 && outbox.pending.size() == 2 && outbox.failures.isEmpty(), "Rejected heartbeat consumed an event");
            SystemClock.time += 15000; tick();
            check(Transport.heartbeats == 2 && outbox.delivered.size() == 2, "Rejected heartbeat was treated as fresh success");
        });
        test("rejected mid-batch heartbeat stops and preserves remainder", () -> {
            add(20); Transport.delay = 7000; Transport.rejectHeartbeatAt = 2; tick();
            int delivered = outbox.delivered.size();
            check(delivered > 0 && delivered < 20 && outbox.pending.size() == 20-delivered, "Rejected refresh lost queued events");
            check(outbox.failures.isEmpty(), "Unsent events were marked failed");
            SystemClock.time += 15000; tick();
            check(outbox.delivered.size() == 20 && Transport.events == 20, "Retry duplicated or lost an event");
        });
        test("network failure retains the event and stops this batch", () -> {
            add(3); Transport.throwEventAt = 2; tick();
            check(outbox.delivered.size() == 1 && outbox.pending.size() == 2, "Network failure lost pending events");
            check(outbox.failures.get("event-2") == 1 && !outbox.failures.containsKey("event-3"), "Wrong event retried");
            Transport.throwEventAt = 0; SystemClock.time += 15000; tick();
            check(outbox.delivered.size() == 3 && Transport.events == 4, "Network retry did not preserve exactly-once acknowledgement");
        });
        test("invalid acknowledgement retains original body and ID", () -> {
            add(2); Transport.badAckAt = 1; tick();
            check(outbox.delivered.size() == 1 && outbox.pending.containsKey("event-1"), "Bad acknowledgement retired an event");
            check(outbox.pending.get("event-1").body.equals("body:event-1"), "Retry body changed");
            Transport.badAckAt = 0; SystemClock.time += 15000; tick();
            check(outbox.delivered.size() == 2 && outbox.pending.isEmpty(), "Retained event did not retry");
        });
        test("new target receives a heartbeat before its first event", () -> {
            tick();
            Config.current = new Config.Snapshot("https://new.example", "token-new", "phone-new");
            add(1); SystemClock.time += 1000; tick();
            check(Transport.calls.size() == 3, "Unexpected target switch request count");
            check(Transport.calls.get(1).path.endsWith("heartbeat") && Transport.calls.get(1).config.url.equals("https://new.example"), "New target inherited an old heartbeat");
            check(outbox.delivered.size() == 1 && Transport.staleEvents == 0, "New target event arrived before its heartbeat");
        });
        test("token rotation stops old batch and authenticates before resuming", () -> {
            add(3);
            Transport.afterEvent = () -> {
                if (Transport.events == 1) Config.current = new Config.Snapshot("https://old.example", "token-new", "phone");
            };
            tick();
            check(Transport.events == 1 && outbox.pending.size() == 2, "Old credentials continued uploading after rotation");
            tick();
            check(Transport.calls.get(2).path.endsWith("heartbeat") && Transport.calls.get(2).config.token.equals("token-new"), "New credentials were not checked before events");
            check(outbox.delivered.size() == 3, "Token rotation lost queued events");
        });
        test("listener state change refreshes heartbeat within batch", () -> {
            add(2); Transport.afterEvent = () -> { ReceiptListenerService.connected = false; };
            tick();
            check(Transport.heartbeats == 2, "Listener state change was suppressed by cached heartbeat");
            check(Transport.calls.get(2).path.endsWith("heartbeat") && Transport.calls.get(2).body.contains("listener_connected=false"), "Disconnected listener was reported as connected");
        });
        test("heartbeat exception retains all events for the next tick", () -> {
            add(2); Transport.throwHeartbeatAt = 1; tick();
            check(outbox.pending.size() == 2 && Transport.events == 0, "Heartbeat exception consumed an event");
            SystemClock.time += 15000; tick();
            check(Transport.heartbeats == 2 && outbox.delivered.size() == 2, "Heartbeat exception suppressed retry");
        });
        System.out.println("{\"passed\":" + passed + ",\"slow_batch_heartbeats\":" + slowHeartbeats +
                ",\"slow_batch_max_heartbeat_gap_ms\":" + slowGap + ",\"slow_batch_stale_events\":0}");
    }
}
