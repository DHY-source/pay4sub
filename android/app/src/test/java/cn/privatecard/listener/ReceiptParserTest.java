package cn.privatecard.listener;

import org.junit.Test;
import static org.junit.Assert.*;

public class ReceiptParserTest {
    @Test public void acceptsWechatCollection() {
        ReceiptParser.Receipt receipt = ReceiptParser.parse(ReceiptParser.WECHAT, "微信支付", "微信支付收款10.00元");
        assertNotNull(receipt); assertEquals("wechat", receipt.channel); assertEquals(1000, receipt.amountCents);
    }
    @Test public void acceptsAlipayCollectionExactly() {
        ReceiptParser.Receipt receipt = ReceiptParser.parse(ReceiptParser.ALIPAY, "支付宝", "你已成功收款0.01元。");
        assertNotNull(receipt); assertEquals("alipay", receipt.channel); assertEquals(1, receipt.amountCents);
    }
    @Test public void acceptsOptionalCurrencyWhitespaceAndSingleDecimal() {
        ReceiptParser.Receipt receipt = ReceiptParser.parse(ReceiptParser.ALIPAY, "收款通知", "支付宝收款 ￥ 8.5 元");
        assertNotNull(receipt); assertEquals(850, receipt.amountCents);
    }
    @Test public void rejectsUntrustedPackageAndOrdinaryChat() {
        assertNull(ReceiptParser.parse("evil.app", "微信支付", "微信支付收款10.00元"));
        assertNull(ReceiptParser.parse(ReceiptParser.WECHAT, "朋友", "微信支付收款10.00元"));
        assertNull(ReceiptParser.parse(ReceiptParser.WECHAT, "微信支付", "朋友：微信支付收款10.00元"));
    }
    @Test public void rejectsUncertainOutgoingRefundAndPending() {
        String[] messages = {"你已付款10.00元", "收到转账10.00元，请确认收款", "待确认收款10元",
                "退款到账10.00元", "微信支付收款10.00元，付款人请求退款", "转账到账10.00元", "红包收入10元"};
        for (String text : messages) {
            assertNull(text, ReceiptParser.parse(ReceiptParser.WECHAT, "微信支付", text));
            assertNull(text, ReceiptParser.parse(ReceiptParser.ALIPAY, "支付宝", text));
        }
    }
    @Test public void rejectsMalformedMoney() {
        String[] amounts = {"0", "0.00", "-1", "1.001", "1e3", "1,000", "10000.01", "99999999", "NaN"};
        for (String amount : amounts) assertNull(amount, ReceiptParser.parse(ReceiptParser.WECHAT, "微信支付", "微信支付收款" + amount + "元"));
    }
    @Test public void rejectsAdditionalLinesAndUnknownNotifications() {
        assertNull(ReceiptParser.parse(ReceiptParser.WECHAT, "微信支付", "微信支付收款10元\n请点击链接"));
        assertNull(ReceiptParser.parse(ReceiptParser.ALIPAY, "营销消息", "成功收款10元"));
        assertNull(ReceiptParser.parse(ReceiptParser.ALIPAY, "支付宝", null));
        assertNull(ReceiptParser.parse(null, null, null));
    }
    @Test public void parsesBoundaryWithoutFloatingPointRounding() {
        assertEquals(1000000L, ReceiptParser.parse(ReceiptParser.WECHAT, "微信支付", "微信支付收款10000.00元").amountCents);
        assertEquals(29L, ReceiptParser.parse(ReceiptParser.WECHAT, "微信支付", "微信支付收款0.29元").amountCents);
    }
}
