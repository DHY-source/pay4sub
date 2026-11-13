package cn.privatecard.listener;

import android.app.Activity;
import android.graphics.Rect;
import android.os.Build;
import android.view.DisplayCutout;
import android.view.View;
import android.view.ViewTreeObserver;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;

/** Owns immersive mode and the root's cutout/keyboard padding. */
@SuppressWarnings("deprecation")
final class FullscreenController {
    interface KeyboardVisibilityListener {
        void onKeyboardVisibilityChanged(boolean visible);
    }

    private final Window window;
    private final View root;
    private final int initialLeft;
    private final int initialTop;
    private final int initialRight;
    private final int initialBottom;
    private final Rect visibleFrame = new Rect();
    private final int[] rootLocation = new int[2];
    private boolean keyboardVisible;
    private boolean disposed;
    private int safeLeft;
    private int safeTop;
    private int safeRight;
    private int safeBottom;
    private int legacyBottomInset;
    private KeyboardVisibilityListener keyboardListener;

    private final Runnable hideBars = new Runnable() {
        @Override public void run() {
            if (disposed || keyboardVisible || !root.hasWindowFocus()) return;
            if (Build.VERSION.SDK_INT >= 30) {
                Api30.hideSystemBars(window);
            } else {
                int appearance = window.getDecorView().getSystemUiVisibility()
                        & (View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
                window.getDecorView().setSystemUiVisibility(
                        appearance | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
            }
        }
    };
    private final Runnable revealFocusedInput = new Runnable() {
        @Override public void run() {
            if (disposed || !keyboardVisible) return;
            View focused = root.findFocus();
            if (focused == null || focused == root || !focused.isShown()) return;
            Rect bounds = new Rect();
            focused.getDrawingRect(bounds);
            focused.requestRectangleOnScreen(bounds, false);
        }
    };
    private final ViewTreeObserver.OnGlobalLayoutListener legacyLayoutListener =
            new ViewTreeObserver.OnGlobalLayoutListener() {
                @Override public void onGlobalLayout() { updateLegacyInsets(); }
            };

    FullscreenController(Activity activity, View root) {
        this.window = activity.getWindow();
        this.root = root;
        initialLeft = root.getPaddingLeft();
        initialTop = root.getPaddingTop();
        initialRight = root.getPaddingRight();
        initialBottom = root.getPaddingBottom();
        root.setFitsSystemWindows(false);
        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override public WindowInsets onApplyWindowInsets(View view, WindowInsets insets) {
                if (disposed) return insets;
                if (Build.VERSION.SDK_INT >= 30) {
                    Api30.applyInsets(FullscreenController.this, insets);
                } else {
                    if (Build.VERSION.SDK_INT >= 28) Api28.readCutout(FullscreenController.this, insets);
                    legacyBottomInset = insets.getSystemWindowInsetBottom();
                    updateLegacyInsets();
                }
                return insets;
            }
        });
        if (Build.VERSION.SDK_INT < 30) {
            root.getViewTreeObserver().addOnGlobalLayoutListener(legacyLayoutListener);
        }
    }

    void setKeyboardVisibilityListener(KeyboardVisibilityListener listener) {
        keyboardListener = listener;
        if (listener != null) listener.onKeyboardVisibilityChanged(keyboardVisible);
    }

    void apply() {
        if (disposed) return;
        // FLAG_FULLSCREEN can disable adjustResize on older Android versions.
        // System UI flags / WindowInsetsController hide the bars instead.
        // FLAG_SECURE is deliberately left untouched.
        window.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN
                | WindowManager.LayoutParams.FLAG_TRANSLUCENT_STATUS
                | WindowManager.LayoutParams.FLAG_TRANSLUCENT_NAVIGATION);
        window.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        if (Build.VERSION.SDK_INT >= 30) {
            Api30.configureWindow(window);
        } else if (Build.VERSION.SDK_INT >= 28) {
            Api28.configureWindow(window);
        }
        root.requestApplyInsets();
        scheduleHideBars();
    }

    void onWindowFocusChanged(boolean hasFocus) {
        if (hasFocus) apply();
    }

    void dispose() {
        disposed = true;
        keyboardListener = null;
        root.removeCallbacks(hideBars);
        root.removeCallbacks(revealFocusedInput);
        root.setOnApplyWindowInsetsListener(null);
        if (Build.VERSION.SDK_INT < 30 && root.getViewTreeObserver().isAlive()) {
            root.getViewTreeObserver().removeOnGlobalLayoutListener(legacyLayoutListener);
        }
    }

    private void scheduleHideBars() {
        root.removeCallbacks(hideBars);
        if (!disposed && !keyboardVisible) root.post(hideBars);
    }

    private void updateLegacyInsets() {
        if (disposed || root.getHeight() == 0) return;
        root.getWindowVisibleDisplayFrame(visibleFrame);
        root.getLocationOnScreen(rootLocation);
        int threshold = Math.max(Math.round(80 * root.getResources().getDisplayMetrics().density),
                root.getRootView().getHeight() / 6);
        int overlap = Math.max(0, rootLocation[1] + root.getHeight() - visibleFrame.bottom);
        boolean visible = overlap > threshold || legacyBottomInset > threshold;
        // If adjustResize already shortened the root, overlap is zero: never
        // add the full keyboard height twice. Padding does not change root height.
        int keyboardBottom = visible && overlap > threshold ? overlap : 0;
        applyPaddingAndKeyboard(safeLeft, safeTop, safeRight, safeBottom, keyboardBottom, visible);
    }

    private void applyPaddingAndKeyboard(int left, int top, int right, int bottom,
            int keyboardBottom, boolean visible) {
        int paddedLeft = initialLeft + left;
        int paddedTop = initialTop + top;
        int paddedRight = initialRight + right;
        int paddedBottom = initialBottom + Math.max(bottom, keyboardBottom);
        boolean paddingChanged = root.getPaddingLeft() != paddedLeft
                || root.getPaddingTop() != paddedTop
                || root.getPaddingRight() != paddedRight
                || root.getPaddingBottom() != paddedBottom;
        boolean visibilityChanged = keyboardVisible != visible;
        if (paddingChanged) root.setPadding(paddedLeft, paddedTop, paddedRight, paddedBottom);
        keyboardVisible = visible;
        if (visibilityChanged && keyboardListener != null) {
            keyboardListener.onKeyboardVisibilityChanged(visible);
        }
        if (visible) {
            root.removeCallbacks(hideBars);
            if (paddingChanged || visibilityChanged) {
                root.removeCallbacks(revealFocusedInput);
                root.post(revealFocusedInput);
            }
        } else if (visibilityChanged) {
            // Reapply only after IME dismissal, not each transient-bar inset
            // update: users must still be able to swipe the system bars open.
            scheduleHideBars();
        }
    }

    private static final class Api28 {
        static void configureWindow(Window window) {
            WindowManager.LayoutParams params = window.getAttributes();
            params.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            window.setAttributes(params);
        }

        static void readCutout(FullscreenController owner, WindowInsets insets) {
            DisplayCutout cutout = insets.getDisplayCutout();
            owner.safeLeft = cutout == null ? 0 : cutout.getSafeInsetLeft();
            owner.safeTop = cutout == null ? 0 : cutout.getSafeInsetTop();
            owner.safeRight = cutout == null ? 0 : cutout.getSafeInsetRight();
            owner.safeBottom = cutout == null ? 0 : cutout.getSafeInsetBottom();
        }
    }

    private static final class Api30 {
        static void configureWindow(Window window) {
            window.setDecorFitsSystemWindows(false);
            WindowManager.LayoutParams params = window.getAttributes();
            params.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS;
            window.setAttributes(params);
        }

        static void hideSystemBars(Window window) {
            WindowInsetsController controller = window.getInsetsController();
            if (controller == null) return;
            controller.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            controller.hide(WindowInsets.Type.systemBars());
        }

        static void applyInsets(FullscreenController owner, WindowInsets insets) {
            android.graphics.Insets safe = insets.getInsetsIgnoringVisibility(
                    WindowInsets.Type.displayCutout() | WindowInsets.Type.captionBar());
            boolean visible = insets.isVisible(WindowInsets.Type.ime());
            int keyboardBottom = visible ? insets.getInsets(WindowInsets.Type.ime()).bottom : 0;
            owner.applyPaddingAndKeyboard(safe.left, safe.top, safe.right, safe.bottom, keyboardBottom, visible);
        }
    }
}
