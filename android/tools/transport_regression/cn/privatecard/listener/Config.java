package cn.privatecard.listener;
final class Config {
    static final class Snapshot {
        final String url, token, deviceId;
        Snapshot(String url, String token, String deviceId) {
            this.url = url; this.token = token; this.deviceId = deviceId;
        }
    }
}
