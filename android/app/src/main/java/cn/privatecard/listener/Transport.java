package cn.privatecard.listener;

import java.io.InputStream;
import java.io.OutputStream;
import java.io.ByteArrayOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

final class Transport {
    static Reply post(Config.Snapshot config, String path, String body) throws Exception {
        String timestamp = Long.toString(System.currentTimeMillis() / 1000L);
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        String signature = Protocol.sign(config.token, timestamp, bytes);
        HttpURLConnection connection = (HttpURLConnection) new URL(config.url + path).openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(8000);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setUseCaches(false);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setRequestProperty("X-Device-Timestamp", timestamp);
            connection.setRequestProperty("X-Device-Signature", signature);
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream output = connection.getOutputStream()) { output.write(bytes); }
            int status = connection.getResponseCode();
            InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            ByteArrayOutputStream response = new ByteArrayOutputStream();
            if (stream != null) {
                try (InputStream input = stream) {
                    byte[] buffer = new byte[1024];
                    int total = 0;
                    int read;
                    while ((read = input.read(buffer)) != -1) {
                        total += read;
                        if (total > 65536) throw new IllegalStateException("响应长度超限");
                        response.write(buffer, 0, read);
                    }
                }
            }
            JSONObject json = status == 200 ? new JSONObject(response.toString(StandardCharsets.UTF_8.name())) : null;
            return new Reply(status, json);
        } finally { connection.disconnect(); }
    }
    static final class Reply {
        final int status; final JSONObject body;
        Reply(int status, JSONObject body) { this.status = status; this.body = body; }
        boolean ok() { return status == 200 && body != null && body.optBoolean("ok", false); }
        boolean acknowledges(String id) {
            if (!ok()) return false;
            String result = body.optString("result", "");
            String eventId = body.optString("event_id", "");
            return id.equals(eventId) && ("delivered".equals(result) ||
                    "review".equals(result) || "duplicate".equals(result));
        }
    }
}
