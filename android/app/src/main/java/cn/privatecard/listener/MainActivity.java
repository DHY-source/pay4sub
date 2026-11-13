package cn.privatecard.listener;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Intent;
import android.content.res.ColorStateList;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputFilter;
import android.text.InputType;
import android.text.TextUtils;
import android.text.method.HideReturnsTransformationMethod;
import android.text.method.PasswordTransformationMethod;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import java.text.DateFormat;
import java.util.Date;

/** Native dashboard; payment and transport logic remain in their own components. */
public final class MainActivity extends Activity {
    private static final int INK = 0xFF11263D, MUTED = 0xFF718096, ACCENT = 0xFF0F9F8F;
    private static final int PALE = 0xFFEAF7F4, SURFACE = 0xFFF4F6F8, LINE = 0xFFE5EBF0, WHITE = Color.WHITE;
    private Config config;
    private EditText server, token;
    private boolean tokenVisible;
    private TextView heroTitle, heroDescription, heroPill, permissionValue, queueValue;
    private TextView activityMessage, activityTime, siteSummary;
    private Button heroAction;
    private LinearLayout bottomNavigation;
    private final View[] pages = new View[3];
    private final LinearLayout[] navigationItems = new LinearLayout[3];
    private final Glyph[] navigationIcons = new Glyph[3];
    private final TextView[] navigationLabels = new TextView[3];
    private FullscreenController fullscreen;
    private int activePage;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable refresh = new Runnable() {
        @Override public void run() { updateStatus(); handler.postDelayed(this, 2000); }
    };
    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        config = new Config(this);
        FrameLayout root = new FrameLayout(this); root.setBackgroundColor(SURFACE);
        LinearLayout shell = column(); root.addView(shell, new FrameLayout.LayoutParams(-1, -1));
        FrameLayout pageHost = new FrameLayout(this);
        shell.addView(pageHost, new LinearLayout.LayoutParams(-1, 0, 1));
        pages[0] = dashboard(); pages[1] = connectionPage(); pages[2] = helpPage();
        for (View page : pages) pageHost.addView(page, new FrameLayout.LayoutParams(-1, -1));
        bottomNavigation = navigation(); shell.addView(bottomNavigation, new LinearLayout.LayoutParams(-1, -2));
        setContentView(root);
        fullscreen = new FullscreenController(this, root);
        fullscreen.setKeyboardVisibilityListener(visible -> bottomNavigation.setVisibility(visible ? View.GONE : View.VISIBLE));
        showPage(Math.max(0, Math.min(state == null ? 0 : state.getInt("active_page", 0), 2)));
        UploadWorker.get(this); updateStatus();
    }
    @Override protected void onResume() {
        super.onResume(); handler.removeCallbacks(refresh); handler.post(refresh);
        if (fullscreen != null) fullscreen.apply();
    }
    @Override protected void onPause() { handler.removeCallbacks(refresh); super.onPause(); }
    @Override public void onWindowFocusChanged(boolean focused) {
        super.onWindowFocusChanged(focused);
        if (fullscreen != null) fullscreen.onWindowFocusChanged(focused);
    }
    @Override protected void onDestroy() {
        handler.removeCallbacks(refresh); if (fullscreen != null) fullscreen.dispose(); super.onDestroy();
    }
    @Override protected void onSaveInstanceState(Bundle state) {
        state.putInt("active_page", activePage);
        // Unsaved credentials and device secrets never enter the state bundle.
        super.onSaveInstanceState(state);
    }
    private View dashboard() {
        LinearLayout body = column(); ScrollView page = page(body);
        header(body, "到账助手", "个人收款设备 · 安全转发", "pulse");
        LinearLayout hero = column(); hero.setPadding(dp(22), dp(24), dp(22), dp(22));
        GradientDrawable gradient = new GradientDrawable(GradientDrawable.Orientation.TL_BR, new int[]{INK, 0xFF164D59});
        gradient.setCornerRadius(dp(24)); hero.setBackground(gradient);
        LinearLayout top = row(); top.addView(label("通知监听", 12, 0xFFB9D3DC, true), new LinearLayout.LayoutParams(0, -2, 1));
        heroPill = label("待设置", 11, 0xFFFFD898, true); heroPill.setPadding(dp(10), dp(5), dp(10), dp(5));
        heroPill.setBackground(rounded(0xFF36515C, 50, 0)); top.addView(heroPill); hero.addView(top);
        heroTitle = label("连接你的收款设备", 27, WHITE, true); append(hero, heroTitle, 18);
        heroDescription = label("完成设置后，收款通知将在这里有序流转。", 13, 0xFFC6DCE2, false);
        heroDescription.setLineSpacing(dp(4), 1); append(hero, heroDescription, 8);
        heroAction = button("开始连接  →", false, view -> performPrimaryAction());
        heroAction.setTextColor(INK); heroAction.setBackground(ripple(WHITE, 14, 0)); append(hero, heroAction, 22);
        append(body, hero, 24);
        LinearLayout statistics = row();
        LinearLayout permission = metric("shield", "通知访问", "待授权"); permissionValue = (TextView) permission.getChildAt(1);
        LinearLayout queue = metric("queue", "待上报", "0 条"); queueValue = (TextView) queue.getChildAt(1);
        LinearLayout.LayoutParams left = new LinearLayout.LayoutParams(0, -2, 1); left.rightMargin = dp(6);
        LinearLayout.LayoutParams right = new LinearLayout.LayoutParams(0, -2, 1); right.leftMargin = dp(6);
        statistics.addView(permission, left); statistics.addView(queue, right); append(body, statistics, 14);
        section(body, "快捷操作", "从连接到上报，一步可达");
        LinearLayout actions = card();
        siteSummary = actionRow(actions, "link", "连接设置", "配置站点地址和设备密钥", view -> showPage(1), false);
        actionRow(actions, "shield", "通知使用权", "检查系统授权与监听状态", view -> openNotifications(), true);
        actionRow(actions, "retry", "重试待上报", "保留原始时间，重新尝试上传", view -> retryPending(), true); append(body, actions, 12);
        section(body, "最近活动", "连接状态以网页后台为准");
        LinearLayout activity = card(); activity.setPadding(dp(18), dp(18), dp(18), dp(18));
        LinearLayout heading = row(); heading.addView(new Glyph("pulse", ACCENT), square(20));
        TextView title = label("设备动态", 13, INK, true); LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(-2, -2); tp.leftMargin = dp(8);
        heading.addView(title, tp); activity.addView(heading);
        activityMessage = label("尚未上报", 14, INK, false); activityMessage.setLineSpacing(dp(5), 1);
        activityMessage.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE); append(activity, activityMessage, 14);
        activityTime = label("等待首次连接", 11, MUTED, false); append(activity, activityTime, 8); append(body, activity, 12);
        LinearLayout providers = row(); providers.setGravity(Gravity.CENTER);
        providers.addView(provider("支", "支付宝", 0xFF287BD3)); providers.addView(new View(this), new LinearLayout.LayoutParams(dp(22), 1));
        providers.addView(provider("微", "微信支付", 0xFF2B9B65)); append(body, providers, 25);
        TextView note = label("仅转发匹配的收款通知 · 请在真实账单中核实到账", 11, MUTED, false); note.setGravity(Gravity.CENTER); append(body, note, 12);
        return page;
    }
    private View connectionPage() {
        LinearLayout body = column(); ScrollView page = page(body);
        header(body, "连接设置", "让这台手机与你的发卡站点保持连接", "link");
        LinearLayout settings = card(); settings.setPadding(dp(20), dp(22), dp(20), dp(22));
        settings.addView(label("站点凭据", 18, INK, true)); append(settings, label("密钥只保存在本机，请勿分享给买家。", 12, MUTED, false), 6);
        append(settings, label("服务器地址", 13, INK, true), 24);
        server = field("https://cards.example.com", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        server.setText(config.url()); server.setImeOptions(EditorInfo.IME_ACTION_NEXT); append(settings, inputShell(server, "link", null), 9);
        append(settings, label("填写 HTTPS 网站根地址，无需加 /admin。", 11, MUTED, false), 7);
        append(settings, label("设备密钥", 13, INK, true), 21);
        token = field("粘贴服务器生成的设备密钥", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        token.setFilters(new InputFilter[]{new InputFilter.LengthFilter(256)}); token.setTransformationMethod(PasswordTransformationMethod.getInstance());
        token.setImeOptions(EditorInfo.IME_ACTION_DONE); server.setNextFocusForwardId(token.getId()); server.setNextFocusDownId(token.getId());
        try { token.setText(config.token()); } catch (Exception ignored) { toast("原设备密钥无法读取，请重新填写"); }
        Button reveal = smallButton("显示", view -> toggleToken((Button) view)); reveal.setContentDescription("显示设备密钥");
        append(settings, inputShell(token, "key", reveal), 9); append(settings, label("对应服务器配置中的 DEVICE_TOKEN。", 11, MUTED, false), 7);
        token.setOnEditorActionListener((view, action, event) -> { if (action == EditorInfo.IME_ACTION_DONE) { saveAndTest(); return true; } return false; });
        append(settings, button("保存并检查连接", true, view -> saveAndTest()), 25);
        append(settings, button("仅保存配置", false, view -> { if (save()) { hideKeyboard(); updateStatus(); } }), 10); append(body, settings, 24);
        LinearLayout identity = card(); identity.setPadding(dp(20), dp(20), dp(20), dp(20)); identity.addView(label("这台设备", 16, INK, true));
        append(identity, label("用于在你的站点中识别当前手机", 12, MUTED, false), 5);
        TextView id = label(config.deviceId(), 12, MUTED, false); id.setTypeface(Typeface.MONOSPACE); id.setTextIsSelectable(true); append(identity, id, 14);
        append(identity, button("复制设备 ID", false, view -> {
            ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (clipboard != null) { clipboard.setPrimaryClip(ClipData.newPlainText("设备 ID", config.deviceId())); toast("设备 ID 已复制"); }
        }), 14); append(body, identity, 16);
        append(body, callout("shield", "凭据有保护", "设备密钥通过 Android Keystore 加密保存。此应用保持截图与后台预览保护，不读取联系人。", PALE, ACCENT), 16);
        return page;
    }
    private View helpPage() {
        LinearLayout body = column(); ScrollView page = page(body); header(body, "运行与帮助", "让备用机持续、稳定地处理收款通知", "guide");
        LinearLayout setup = card(); setup.setPadding(dp(20), dp(20), dp(20), dp(20)); setup.addView(label("三个运行准备", 18, INK, true));
        step(setup, "01", "授予通知使用权", "允许本应用读取微信、支付宝的收款通知。", "打开通知设置", view -> openNotifications());
        step(setup, "02", "允许后台运行", "保持联网、充电和自动时间；按手机要求放宽电池限制。", "查看电池设置", view -> launch(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)));
        step(setup, "03", "检查应用设置", "重启后确认监听已连接；部分系统需要允许自启动。", "打开应用设置", view -> launch(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())))); append(body, setup, 24);
        section(body, "遇到问题时", "先核对真实账单，再处理订单");
        LinearLayout questions = card(); questions.setPadding(dp(20), dp(6), dp(20), dp(8));
        explanation(questions, "监听已授权，但未连接？", "从系统通知使用权设置关闭再开启授权，检查后台限制，然后返回状态页。", false);
        explanation(questions, "出现 HTTP 401？", "核对服务器设备密钥，并开启手机自动日期和时间。配置正确后点击检查连接。", true);
        explanation(questions, "已收款却没有自动发卡？", "检查通知是否显示、网页后台是否在线以及待核查原因。请勿重复付款或放宽金额匹配规则。", true); append(body, questions, 12);
        append(body, callout("info", "先完成真实小额测试", "通知监听不是官方支付回调，无法独立证明到账。先在网页后台使用人工模式，测试通过后再启用自动发卡。", 0xFFFFF5E5, 0xFF9E702A), 18);
        TextView footer = label("到账助手  " + versionName() + "\n个人自用 · 原生轻量 · 不上传其他应用通知", 11, MUTED, false);
        footer.setGravity(Gravity.CENTER); footer.setLineSpacing(dp(6), 1); append(body, footer, 26); return page;
    }
    private void performPrimaryAction() {
        if (!config.configured()) { showPage(1); return; }
        if (!hasNotificationPermission()) { openNotifications(); return; }
        UploadWorker.get(this).test(); toast("正在使用已保存配置发送心跳，不会生成模拟付款");
    }
    private void saveAndTest() {
        if (!save()) return; hideKeyboard(); UploadWorker.get(this).test(); showPage(0); toast("已安排连接检查，请查看最近活动");
    }
    private boolean save() {
        try {
            String currentUrl = config.url(), newUrl = Config.normalizeUrl(server.getText().toString());
            if (!currentUrl.isEmpty() && !currentUrl.equals(newUrl) && Outbox.get(this).pendingCount() > 0) {
                new AlertDialog.Builder(this).setTitle("还有待上报的收款通知").setMessage("请先处理原服务器的待上报通知，再更改地址，避免漏单。").setPositiveButton("知道了", null).show(); return false;
            }
            config.save(newUrl, token.getText().toString()); toast("配置已保存"); return true;
        } catch (Exception error) {
            new AlertDialog.Builder(this).setTitle("请检查连接配置").setMessage(error.getMessage()).setPositiveButton("继续编辑", null).show(); return false;
        }
    }
    private void retryPending() {
        if (!config.configured()) { toast("请先保存连接配置"); showPage(1); return; }
        Outbox.get(this).retryNow(); UploadWorker.get(this).test(); toast("已安排重试，原始通知时间和 ID 保持不变"); updateStatus();
    }
    private void updateStatus() {
        if (heroTitle == null) return;
        boolean configured = config.configured(), permission = hasNotificationPermission(), connected = ReceiptListenerService.connected;
        setText(permissionValue, permission ? "已授权" : "待授权"); permissionValue.setTextColor(permission ? ACCENT : 0xFFB78538);
        setText(queueValue, Outbox.get(this).pendingCount() + " 条");
        if (!configured) {
            setText(heroPill, "待设置"); setText(heroTitle, "连接你的收款设备"); setText(heroDescription, "设置站点与密钥，让到账通知找到正确的去处。"); setText(heroAction, "开始连接  →");
        } else if (!permission) {
            setText(heroPill, "待授权"); setText(heroTitle, "还差一步，即可监听"); setText(heroDescription, "开启通知访问权限，接收指定格式的收款提醒。"); setText(heroAction, "开启通知访问  →");
        } else if (connected) {
            setText(heroPill, "已连接"); setText(heroTitle, "通知监听已就绪"); setText(heroDescription, "正在等待匹配的收款通知。站点连接请查看最近活动。"); setText(heroAction, "检查服务器连接  →");
        } else {
            setText(heroPill, "等待中"); setText(heroTitle, "等待系统连接监听"); setText(heroDescription, "通知权限已授予，请检查系统后台限制与应用设置。"); setText(heroAction, "检查服务器连接  →");
        }
        heroPill.setTextColor(configured && permission && connected ? 0xFFB1F1DE : 0xFFFFD898);
        String host = configured ? Uri.parse(config.url()).getHost() : null;
        setText(siteSummary, host == null ? "配置站点地址和设备密钥" : host);
        setText(activityMessage, config.lastStatus()); long last = config.statusTime();
        setText(activityTime, last > 0 ? "更新于 " + DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.MEDIUM).format(new Date(last)) : "等待首次连接");
    }
    private void toggleToken(Button reveal) {
        int position = token.getSelectionStart(); tokenVisible = !tokenVisible;
        token.setTransformationMethod(tokenVisible ? HideReturnsTransformationMethod.getInstance() : PasswordTransformationMethod.getInstance());
        token.setSelection(Math.max(0, Math.min(position, token.length()))); reveal.setText(tokenVisible ? "隐藏" : "显示"); reveal.setContentDescription(tokenVisible ? "隐藏设备密钥" : "显示设备密钥");
    }
    private boolean hasNotificationPermission() {
        String enabled = Settings.Secure.getString(getContentResolver(), "enabled_notification_listeners"); if (enabled == null) return false;
        ComponentName expected = new ComponentName(this, ReceiptListenerService.class);
        for (String item : enabled.split(":")) if (expected.equals(ComponentName.unflattenFromString(item))) return true; return false;
    }
    private void openNotifications() { launch(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)); }
    private void launch(Intent intent) { try { startActivity(intent); } catch (Exception ignored) { toast("请到系统设置中手动打开对应页面"); } }
    private void hideKeyboard() {
        InputMethodManager input = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        if (input != null && getCurrentFocus() != null) input.hideSoftInputFromWindow(getCurrentFocus().getWindowToken(), 0);
        server.clearFocus(); token.clearFocus();
    }
    private String versionName() { try { return getPackageManager().getPackageInfo(getPackageName(), 0).versionName; } catch (Exception ignored) { return ""; } }
    private void toast(String text) { Toast.makeText(this, text, Toast.LENGTH_SHORT).show(); }
    private void setText(TextView view, String text) { if (!TextUtils.equals(view.getText(), text)) view.setText(text); }
    private ScrollView page(LinearLayout body) {
        ScrollView page = new ScrollView(this); page.setFillViewport(true); page.setClipToPadding(false); page.setVerticalScrollBarEnabled(false); page.setOverScrollMode(View.OVER_SCROLL_IF_CONTENT_SCROLLS);
        body.setPadding(dp(20), dp(26), dp(20), dp(24)); body.setFocusableInTouchMode(true); page.addView(body, new ScrollView.LayoutParams(-1, -2));
        page.addOnLayoutChangeListener((view, left, top, right, bottom, oldLeft, oldTop, oldRight, oldBottom) -> {
            int horizontal = Math.max(dp(20), (right - left - dp(640)) / 2);
            if (body.getPaddingLeft() != horizontal) body.setPadding(horizontal, dp(26), horizontal, dp(24));
        }); return page;
    }
    private void header(LinearLayout body, String title, String subtitle, String icon) {
        LinearLayout heading = row(); FrameLayout mark = new FrameLayout(this); mark.setBackground(rounded(INK, 15, 0));
        mark.addView(new Glyph(icon, 0xFF9CE7D8), new FrameLayout.LayoutParams(dp(26), dp(26), Gravity.CENTER)); heading.addView(mark, square(48));
        LinearLayout words = column(); words.addView(label(title, 25, INK, true)); append(words, label(subtitle, 12, MUTED, false), 4);
        LinearLayout.LayoutParams textParams = new LinearLayout.LayoutParams(0, -2, 1); textParams.leftMargin = dp(13); heading.addView(words, textParams); body.addView(heading);
    }
    private LinearLayout metric(String icon, String title, String value) {
        LinearLayout block = card(); block.setPadding(dp(17), dp(18), dp(17), dp(18)); LinearLayout description = row();
        description.addView(new Glyph(icon, MUTED), square(17)); TextView text = label(title, 12, MUTED, false);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-2, -2); params.leftMargin = dp(7); description.addView(text, params); block.addView(description);
        append(block, label(value, 23, INK, true), 10); return block;
    }
    private void section(LinearLayout body, String title, String subtitle) { append(body, label(title, 18, INK, true), 27); append(body, label(subtitle, 11, MUTED, false), 4); }
    private TextView actionRow(LinearLayout parent, String icon, String title, String subtitle, View.OnClickListener action, boolean separator) {
        if (separator) divider(parent); LinearLayout item = row(); item.setPadding(dp(17), dp(15), dp(17), dp(15)); item.setBackground(ripple(WHITE, 18, 0)); item.setMinimumHeight(dp(76));
        item.setOnClickListener(action); item.setFocusable(true); item.setContentDescription(title + "，" + subtitle);
        FrameLayout bubble = new FrameLayout(this); bubble.setBackground(rounded(PALE, 12, 0)); bubble.addView(new Glyph(icon, ACCENT), new FrameLayout.LayoutParams(dp(21), dp(21), Gravity.CENTER)); item.addView(bubble, square(42));
        LinearLayout text = column(); text.addView(label(title, 14, INK, true)); TextView hint = label(subtitle, 11, MUTED, false); append(text, hint, 4);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1); params.setMargins(dp(13), 0, dp(10), 0); item.addView(text, params);
        item.addView(new Glyph("chevron", 0xFFA4AFBE), square(17)); parent.addView(item, new LinearLayout.LayoutParams(-1, -2)); return hint;
    }
    private LinearLayout provider(String initial, String title, int color) {
        LinearLayout provider = row(); TextView badge = label(initial, 11, WHITE, true); badge.setGravity(Gravity.CENTER); badge.setBackground(rounded(color, 7, 0)); provider.addView(badge, square(23));
        TextView name = label(title, 12, MUTED, false); LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-2, -2); params.leftMargin = dp(7); provider.addView(name, params); return provider;
    }
    private LinearLayout inputShell(EditText input, String icon, Button trailing) {
        LinearLayout shell = row(); shell.setPadding(dp(13), dp(3), dp(9), dp(3)); shell.setBackground(rounded(0xFFFAFCFD, 12, LINE));
        shell.addView(new Glyph(icon, MUTED), square(19)); shell.addView(input, new LinearLayout.LayoutParams(0, -2, 1)); if (trailing != null) shell.addView(trailing, new LinearLayout.LayoutParams(-2, -2));
        input.setOnFocusChangeListener((view, focused) -> shell.setBackground(rounded(focused ? WHITE : 0xFFFAFCFD, 12, focused ? ACCENT : LINE))); return shell;
    }
    private EditText field(String hint, int type) {
        EditText input = new EditText(this); input.setId(View.generateViewId()); input.setInputType(type); input.setSingleLine(true); input.setTextSize(14); input.setTextColor(INK); input.setHintTextColor(0xFF97A4B4); input.setHint(hint);
        input.setPadding(dp(10), dp(12), dp(6), dp(12)); input.setMinHeight(dp(48)); input.setBackgroundColor(Color.TRANSPARENT); input.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS); input.setSaveEnabled(false); return input;
    }
    private void step(LinearLayout parent, String number, String title, String description, String actionText, View.OnClickListener action) {
        LinearLayout heading = row(); TextView badge = label(number, 11, ACCENT, true); badge.setGravity(Gravity.CENTER); badge.setBackground(rounded(PALE, 8, 0)); heading.addView(badge, square(30));
        TextView name = label(title, 14, INK, true); LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1); params.leftMargin = dp(10); heading.addView(name, params); append(parent, heading, 23);
        append(parent, label(description, 12, MUTED, false), 9); append(parent, button(actionText, false, action), 12);
    }
    private void explanation(LinearLayout parent, String title, String content, boolean separator) {
        if (separator) divider(parent); append(parent, label(title, 14, INK, true), 17); TextView text = label(content, 12, MUTED, false); text.setLineSpacing(dp(5), 1);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.topMargin = dp(8); params.bottomMargin = dp(17); parent.addView(text, params);
    }
    private View callout(String icon, String title, String content, int fill, int color) {
        LinearLayout box = column(); box.setPadding(dp(18), dp(18), dp(18), dp(18)); box.setBackground(rounded(fill, 17, 0)); LinearLayout heading = row(); heading.addView(new Glyph(icon, color), square(19));
        TextView name = label(title, 13, color, true); LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1); params.leftMargin = dp(8); heading.addView(name, params); box.addView(heading);
        TextView text = label(content, 12, color, false); text.setLineSpacing(dp(5), 1); append(box, text, 10); return box;
    }
    private LinearLayout navigation() {
        LinearLayout nav = row(); nav.setGravity(Gravity.CENTER); nav.setBackground(rounded(WHITE, 0, LINE)); nav.setPadding(dp(14), dp(8), dp(14), dp(8));
        String[] names = {"运行状态", "连接设置", "使用帮助"}, icons = {"home", "link", "guide"};
        for (int i = 0; i < names.length; i++) {
            final int index = i; LinearLayout item = column(); item.setGravity(Gravity.CENTER); item.setPadding(dp(8), dp(10), dp(8), dp(9)); item.setMinimumHeight(dp(62));
            Glyph icon = new Glyph(icons[i], MUTED); TextView name = label(names[i], 11, MUTED, false); name.setGravity(Gravity.CENTER); item.addView(icon, square(22)); append(item, name, 5);
            item.setOnClickListener(view -> { hideKeyboard(); showPage(index); }); item.setFocusable(true); item.setContentDescription(names[i]);
            navigationItems[i] = item; navigationIcons[i] = icon; navigationLabels[i] = name;
            LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(0, -2, 1); params.leftMargin = dp(3); params.rightMargin = dp(3); nav.addView(item, params);
        } return nav;
    }
    private void showPage(int index) {
        activePage = index;
        for (int i = 0; i < pages.length; i++) {
            pages[i].setVisibility(i == index ? View.VISIBLE : View.GONE); navigationItems[i].setSelected(i == index); navigationItems[i].setBackground(ripple(i == index ? PALE : WHITE, 14, 0));
            navigationIcons[i].tint(i == index ? ACCENT : MUTED); navigationLabels[i].setTextColor(i == index ? ACCENT : MUTED); navigationLabels[i].setTypeface(Typeface.create(i == index ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL));
        } updateStatus(); if (fullscreen != null) fullscreen.apply();
    }
    private LinearLayout column() { LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.VERTICAL); return view; }
    private LinearLayout row() { LinearLayout view = new LinearLayout(this); view.setOrientation(LinearLayout.HORIZONTAL); view.setGravity(Gravity.CENTER_VERTICAL); return view; }
    private LinearLayout card() { LinearLayout view = column(); view.setBackground(rounded(WHITE, 20, LINE)); return view; }
    private TextView label(String text, float size, int color, boolean medium) {
        TextView label = new TextView(this); label.setText(text); label.setTextSize(size); label.setTextColor(color); label.setTypeface(Typeface.create(medium ? "sans-serif-medium" : "sans-serif", Typeface.NORMAL)); label.setIncludeFontPadding(false); label.setLineSpacing(dp(2), 1); return label;
    }
    private Button button(String title, boolean primary, View.OnClickListener action) {
        Button button = new Button(this); button.setText(title); button.setAllCaps(false); button.setTextSize(14); button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL)); button.setTextColor(primary ? WHITE : ACCENT);
        button.setMinHeight(dp(52)); button.setMinimumHeight(dp(52)); button.setMinWidth(0); button.setMinimumWidth(0); button.setPadding(dp(14), dp(13), dp(14), dp(13)); button.setStateListAnimator(null);
        button.setBackground(ripple(primary ? ACCENT : WHITE, 13, primary ? 0 : LINE)); button.setOnClickListener(action); return button;
    }
    private Button smallButton(String title, View.OnClickListener action) {
        Button button = button(title, false, action); button.setTextSize(12); button.setPadding(dp(9), dp(10), dp(9), dp(10)); button.setBackground(ripple(Color.TRANSPARENT, 9, 0)); button.setMinHeight(dp(48)); button.setMinimumHeight(dp(48)); return button;
    }
    private GradientDrawable rounded(int fill, int radius, int border) {
        GradientDrawable background = new GradientDrawable(); background.setColor(fill); background.setCornerRadius(dp(radius)); if (border != 0) background.setStroke(dp(1), border); return background;
    }
    private RippleDrawable ripple(int fill, int radius, int border) { return new RippleDrawable(ColorStateList.valueOf(0x200F9F8F), rounded(fill, radius, border), rounded(WHITE, radius, 0)); }
    private LinearLayout.LayoutParams square(int side) { return new LinearLayout.LayoutParams(dp(side), dp(side)); }
    private void append(LinearLayout parent, View child, int gap) { LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2); params.topMargin = dp(gap); parent.addView(child, params); }
    private void divider(LinearLayout parent) {
        View line = new View(this); line.setBackgroundColor(LINE); LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, dp(1)); params.leftMargin = dp(17); params.rightMargin = dp(17); parent.addView(line, params);
    }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    /** Vector-like canvas glyphs avoid emoji/font inconsistencies across devices. */
    private final class Glyph extends View {
        private final String kind; private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        Glyph(String kind, int color) {
            super(MainActivity.this); this.kind = kind; paint.setColor(color); paint.setStyle(Paint.Style.STROKE); paint.setStrokeWidth(1.8f); paint.setStrokeCap(Paint.Cap.ROUND); paint.setStrokeJoin(Paint.Join.ROUND); setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        }
        void tint(int color) { paint.setColor(color); invalidate(); }
        @Override protected void onDraw(Canvas canvas) {
            super.onDraw(canvas); canvas.save(); float scale = Math.min(getWidth(), getHeight()) / 24f;
            canvas.translate((getWidth() - 24f * scale) / 2, (getHeight() - 24f * scale) / 2); canvas.scale(scale, scale);
            if ("home".equals(kind)) { path(canvas, 3, 10, 12, 3, 21, 10); path(canvas, 5, 9, 5, 21, 19, 21, 19, 9); path(canvas, 9, 21, 9, 14, 15, 14, 15, 21); }
            else if ("pulse".equals(kind)) { canvas.drawRoundRect(new RectF(3, 3, 21, 21), 5, 5, paint); path(canvas, 5, 13, 8, 13, 10, 8, 14, 17, 16, 12, 19, 12); }
            else if ("link".equals(kind)) { canvas.save(); canvas.rotate(-42, 12, 12); canvas.drawRoundRect(new RectF(3, 8, 13, 16), 4, 4, paint); canvas.drawRoundRect(new RectF(11, 8, 21, 16), 4, 4, paint); canvas.drawLine(9, 12, 15, 12, paint); canvas.restore(); }
            else if ("shield".equals(kind)) { path(canvas, 12, 2, 20, 5, 20, 11, 18, 17, 12, 22, 6, 17, 4, 11, 4, 5, 12, 2); path(canvas, 8, 11, 11, 14, 16, 9); }
            else if ("queue".equals(kind)) { canvas.drawRoundRect(new RectF(4, 3, 20, 21), 3, 3, paint); canvas.drawLine(8, 8, 16, 8, paint); canvas.drawLine(8, 12, 16, 12, paint); canvas.drawLine(8, 16, 12, 16, paint); }
            else if ("retry".equals(kind)) { canvas.drawArc(new RectF(4, 4, 20, 20), -50, 285, false, paint); path(canvas, 20, 4, 20, 10, 14, 10); }
            else if ("key".equals(kind)) { canvas.drawCircle(8, 8, 4.5f, paint); path(canvas, 11, 11, 21, 21); canvas.drawLine(16, 16, 19, 13, paint); canvas.drawLine(19, 19, 22, 16, paint); }
            else if ("guide".equals(kind)) { path(canvas, 12, 5, 7, 3, 3, 4, 3, 20, 8, 19, 12, 21, 16, 19, 21, 20, 21, 4, 17, 3, 12, 5, 12, 21); }
            else if ("chevron".equals(kind)) { path(canvas, 9, 5, 16, 12, 9, 19); }
            else if ("info".equals(kind)) { canvas.drawCircle(12, 12, 9, paint); canvas.drawLine(12, 11, 12, 17, paint); canvas.drawPoint(12, 7, paint); }
            else path(canvas, 5, 12, 10, 17, 19, 7);
            canvas.restore();
        }
        private void path(Canvas canvas, float... points) { Path path = new Path(); path.moveTo(points[0], points[1]); for (int i = 2; i < points.length; i += 2) path.lineTo(points[i], points[i + 1]); canvas.drawPath(path, paint); }
    }
}
