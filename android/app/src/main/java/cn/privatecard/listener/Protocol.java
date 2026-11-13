package cn.privatecard.listener;

import java.nio.charset.StandardCharsets;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Pure Java signing code shared with the executable JVM test vectors. */
final class Protocol {
    static String sign(String token, String timestamp, byte[] body) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(token.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        mac.update((timestamp + ".").getBytes(StandardCharsets.UTF_8));
        return hex(mac.doFinal(body));
    }
    static String hex(byte[] bytes) {
        char[] alphabet = "0123456789abcdef".toCharArray();
        char[] out = new char[bytes.length * 2];
        for (int i = 0; i < bytes.length; i++) {
            out[i * 2] = alphabet[(bytes[i] & 255) >>> 4];
            out[i * 2 + 1] = alphabet[bytes[i] & 15];
        }
        return new String(out);
    }
}
