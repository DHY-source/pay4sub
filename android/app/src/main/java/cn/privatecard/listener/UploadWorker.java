package cn.privatecard.listener;

import android.content.Context;
import android.os.SystemClock;
import org.json.JSONObject;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

final class UploadWorker {
    private static UploadWorker instance;
    private final Config config;
    private final Outbox outbox;
    private final ScheduledExecutorService executor = Executors.newSingleThreadScheduledExecutor();
    private long lastHeartbeat = -60000L;
    private Config.Snapshot heartbeatConfig;
    private boolean heartbeatConnected;
    private UploadWorker(Context context) {
        config = new Config(context);
        outbox = Outbox.get(context);
        executor.scheduleWithFixedDelay(this::tick, 0, 15, TimeUnit.SECONDS);
    }
    static synchronized UploadWorker get(Context context) {
        if (instance == null) instance = new UploadWorker(context.getApplicationContext());
        return instance;
    }
    void kick() { executor.execute(this::tick); }
    void test() { executor.execute(() -> { lastHeartbeat = -60000L; tick(); }); }
    private static boolean sameConfig(Config.Snapshot first, Config.Snapshot second) {
        return first != null && second != null && first.url.equals(second.url) &&
                first.token.equals(second.token) && first.deviceId.equals(second.deviceId);
    }
    private boolean heartbeatIfDue(Config.Snapshot snapshot) throws Exception {
        boolean connected = ReceiptListenerService.connected;
        if (sameConfig(heartbeatConfig, snapshot) && heartbeatConnected == connected &&
                SystemClock.elapsedRealtime() - lastHeartbeat < 45000L) return true;
        JSONObject heartbeat = new JSONObject();
        heartbeat.put("device_id", snapshot.deviceId);
        heartbeat.put("listener_connected", connected);
        Transport.Reply result = Transport.post(snapshot, "/api/device/heartbeat", heartbeat.toString());
        if (!result.ok()) {
            // Retain the last successful target/time. A rejected heartbeat must not
            // authorize uploads or suppress the next retry after configuration changes.
            config.status("心跳被拒绝：HTTP " + result.status + "（检查密钥与手机自动时间）");
            return false;
        }
        lastHeartbeat = SystemClock.elapsedRealtime();
        heartbeatConfig = snapshot;
        heartbeatConnected = connected;
        config.status(connected ? "心跳成功，通知监听已连接" : "服务器连接成功，但通知监听尚未连接");
        return true;
    }
    private void tick() {
        if (!config.configured()) return;
        try {
            Config.Snapshot snapshot = config.snapshot();
            if (!heartbeatIfDue(snapshot)) return;
            for (Outbox.Entry entry : outbox.due(snapshot.url)) {
                // Check between uploads: twenty slow-but-successful requests can
                // otherwise starve the heartbeat past the server's 120-second limit.
                // Do not continue an old-target batch after settings change.
                if (!sameConfig(snapshot, config.snapshot())) return;
                if (!heartbeatIfDue(snapshot)) return;
                try {
                    Transport.Reply result = Transport.post(snapshot, "/api/device/events", entry.body);
                    if (result.acknowledges(entry.id)) {
                        outbox.delivered(entry.id);
                        config.status("到账通知已交给服务器；以管理后台审核/发卡状态为准");
                    } else {
                        outbox.failed(entry, result.status >= 400 && result.status < 500 && result.status != 429);
                        config.status("通知上报失败或回执异常：HTTP " + result.status + "；已保留，稍后重试");
                    }
                } catch (Exception e) {
                    outbox.failed(entry, false);
                    config.status("通知上报网络失败（" + e.getClass().getSimpleName() + "）；已保留重试");
                    break;
                }
            }
        } catch (Exception e) {
            config.status("连接失败（" + e.getClass().getSimpleName() + "）；检查网络与 HTTPS 证书");
        }
    }
}
