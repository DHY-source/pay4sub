package cn.privatecard.listener;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.UUID;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class Config {
    private static final String KEY_ALIAS = "privatecard-device-token-v1";
    private final SharedPreferences preferences;
    Config(Context context) { preferences = context.getSharedPreferences("privatecard", Context.MODE_PRIVATE); }
    String url() { return preferences.getString("server_url", ""); }
    synchronized String deviceId() {
        String id = preferences.getString("device_id", null);
        if (id == null) {
            id = UUID.randomUUID().toString();
            preferences.edit().putString("device_id", id).commit();
        }
        return id;
    }
    String token() throws Exception {
        String encrypted = preferences.getString("token_encrypted", "");
        if (encrypted.isEmpty()) return "";
        String[] parts = encrypted.split(":", 2);
        if (parts.length != 2) throw new IllegalStateException("设备密钥无法读取，请重新保存");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)));
        return new String(cipher.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), StandardCharsets.UTF_8);
    }
    boolean configured() {
        try { return !url().isEmpty() && token().length() >= 32; } catch (Exception ignored) { return false; }
    }
    Snapshot snapshot() throws Exception {
        synchronized (Config.class) { return new Snapshot(url(), token(), deviceId()); }
    }
    static final class Snapshot {
        final String url; final String token; final String deviceId;
        Snapshot(String url, String token, String deviceId) { this.url = url; this.token = token; this.deviceId = deviceId; }
    }
    void save(String rawUrl, String rawToken) throws Exception {
        String normalized = normalizeUrl(rawUrl);
        String token = rawToken.trim();
        if (!token.matches("[0-9a-fA-F]{32,256}"))
            throw new IllegalArgumentException("设备密钥应为服务器生成的 32–256 位十六进制字符");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        String encrypted = Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP) + ":" +
                Base64.encodeToString(cipher.doFinal(token.getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP);
        synchronized (Config.class) {
            if (!preferences.edit().putString("server_url", normalized).putString("token_encrypted", encrypted).commit())
                throw new IllegalStateException("配置保存失败");
        }
    }
    static String normalizeUrl(String rawUrl) throws Exception {
        String raw = rawUrl.trim();
        URI uri = new URI(raw);
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null ||
                uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null ||
                (uri.getRawPath() != null && !uri.getRawPath().isEmpty() && !"/".equals(uri.getRawPath())))
            throw new IllegalArgumentException("请输入 HTTPS 网站根地址，例如 https://cards.example.com（不要带路径或参数）");
        return raw.endsWith("/") ? raw.substring(0, raw.length() - 1) : raw;
    }
    void status(String text) {
        preferences.edit().putString("last_status", text).putLong("last_status_time", System.currentTimeMillis()).apply();
    }
    String lastStatus() { return preferences.getString("last_status", "尚未上报"); }
    long statusTime() { return preferences.getLong("last_status_time", 0); }
    private static synchronized SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(KEY_ALIAS)) {
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            generator.generateKey();
        }
        return (SecretKey) store.getKey(KEY_ALIAS, null);
    }
}
