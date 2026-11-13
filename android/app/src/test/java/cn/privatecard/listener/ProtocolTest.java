package cn.privatecard.listener;

import java.nio.charset.StandardCharsets;
import org.junit.Test;
import static org.junit.Assert.*;

public class ProtocolTest {
    private static final String TOKEN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    @Test public void matchesPythonHmacForAsciiBody() throws Exception {
        byte[] body = "{\"device_id\":\"test\",\"listener_connected\":true}".getBytes(StandardCharsets.UTF_8);
        assertEquals("4b65498bfc49e0aca79658464bfd87e3bfec36efdb16fcdcae92274478eff89e", Protocol.sign(TOKEN, "1700000000", body));
    }
    @Test public void signsExactUtf8Body() throws Exception {
        byte[] body = "{\"text\":\"微信支付收款10.00元\"}".getBytes(StandardCharsets.UTF_8);
        assertEquals("8bf1f1192b3a803cd48686c6419993093effdcf4ea1ab23ec04bd96bc1a8be8a", Protocol.sign(TOKEN, "1700000000", body));
    }
    @Test public void bindsTimestampAndEveryBodyByte() throws Exception {
        byte[] body = "{}".getBytes(StandardCharsets.UTF_8);
        assertNotEquals(Protocol.sign(TOKEN, "1700000000", body), Protocol.sign(TOKEN, "1700000001", body));
        assertNotEquals(Protocol.sign(TOKEN, "1700000000", body), Protocol.sign(TOKEN, "1700000000", "{} ".getBytes(StandardCharsets.UTF_8)));
    }
}
