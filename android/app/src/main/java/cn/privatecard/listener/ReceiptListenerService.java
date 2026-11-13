package cn.privatecard.listener;

import android.app.Notification;
import android.content.ComponentName;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

public final class ReceiptListenerService extends NotificationListenerService {
    static volatile boolean connected;
    @Override public void onListenerConnected() {
        connected = true;
        new Config(this).status("通知监听已连接");
        UploadWorker.get(this).test();
        // Intentionally do not replay getActiveNotifications(): old notifications must not appear new.
    }
    @Override public void onListenerDisconnected() {
        connected = false;
        new Config(this).status("通知监听已断开，请检查系统后台限制");
        UploadWorker.get(this).test();
        requestRebind(new ComponentName(this, ReceiptListenerService.class));
    }
    @Override public void onDestroy() {
        connected = false;
        super.onDestroy();
    }
    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null) return;
        Notification notification = sbn.getNotification();
        if (notification == null || (notification.flags & Notification.FLAG_GROUP_SUMMARY) != 0) return;
        String packageName = sbn.getPackageName();
        if (!ReceiptParser.WECHAT.equals(packageName) && !ReceiptParser.ALIPAY.equals(packageName)) return;
        Bundle extras = notification.extras;
        if (extras == null) return;
        String title = string(extras.getCharSequence(Notification.EXTRA_TITLE));
        String body = string(extras.getCharSequence(Notification.EXTRA_BIG_TEXT));
        if (body.isEmpty()) body = string(extras.getCharSequence(Notification.EXTRA_TEXT));
        ReceiptParser.Receipt receipt = ReceiptParser.parse(packageName, title, body);
        if (receipt == null) return;
        Config config = new Config(this);
        if (!config.configured()) { config.status("识别到收款通知，但尚未配置服务器；请在后台核实这笔款项"); return; }
        try {
            long postTime = sbn.getPostTime();
            long when = notification.when;
            // Preserve old/future source times. Rewriting them to postTime could
            // turn a reposted old receipt into an apparently fresh payment.
            long occurred = when > 0 ? when : postTime;
            String material = sbn.getKey() + "\n" + postTime + "\n" + title + "\n" + body;
            String id = Protocol.hex(MessageDigest.getInstance("SHA-256").digest(material.getBytes(StandardCharsets.UTF_8)));
            JSONObject payload = new JSONObject();
            payload.put("event_id", id); payload.put("device_id", config.deviceId());
            payload.put("channel", receipt.channel); payload.put("amount_cents", receipt.amountCents);
            payload.put("occurred_at", occurred / 1000L); payload.put("source_package", packageName);
            payload.put("title", title); payload.put("text", body);
            if (Outbox.get(this).enqueue(id, config.url(), payload.toString())) {
                config.status("到账通知已保存，等待上报");
                UploadWorker.get(this).kick();
            }
        } catch (Exception e) {
            config.status("通知入队失败（" + e.getClass().getSimpleName() + "）；请在后台人工核对款项");
        }
    }
    private static String string(CharSequence value) { return value == null ? "" : value.toString(); }
}
