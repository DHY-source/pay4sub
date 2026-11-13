package cn.privatecard.listener;

import java.math.BigDecimal;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Fail-closed recognizer. Notification text is not an official settlement proof. */
public final class ReceiptParser {
    public static final String WECHAT = "com.tencent.mm";
    public static final String ALIPAY = "com.eg.android.AlipayGphone";
    private static final String AMOUNT = "\\s*(?:[￥¥]\\s*)?([0-9]{1,7}(?:\\.[0-9]{1,2})?)\\s*元[。！!]?";
    private static final Pattern WX_RECEIPT = Pattern.compile("^微信支付收款" + AMOUNT + "$");
    private static final Pattern ALI_RECEIPT = Pattern.compile("^(?:你已成功收款|支付宝成功收款|支付宝收款|成功收款)" + AMOUNT + "$");
    private static final Set<String> ALI_TITLES = new HashSet<>(Arrays.asList(
            "支付宝", "收款通知", "支付宝收款", "支付宝到账通知"));
    private ReceiptParser() {}

    public static final class Receipt {
        public final String channel;
        public final long amountCents;
        Receipt(String channel, long cents) { this.channel = channel; this.amountCents = cents; }
    }

    public static Receipt parse(String packageName, String title, String text) {
        if (title == null || text == null || text.length() > 512) return null;
        String normalizedTitle = title.trim();
        String normalizedText = text.trim();
        Matcher matcher;
        String channel;
        if (WECHAT.equals(packageName) && "微信支付".equals(normalizedTitle)) {
            matcher = WX_RECEIPT.matcher(normalizedText);
            channel = "wechat";
        } else if (ALIPAY.equals(packageName) && ALI_TITLES.contains(normalizedTitle)) {
            matcher = ALI_RECEIPT.matcher(normalizedText);
            channel = "alipay";
        } else {
            return null;
        }
        if (!matcher.matches()) return null;
        try {
            long cents = new BigDecimal(matcher.group(1)).movePointRight(2).longValueExact();
            if (cents <= 0 || cents > 1000000L) return null;
            return new Receipt(channel, cents);
        } catch (ArithmeticException | NumberFormatException e) {
            return null;
        }
    }
}
