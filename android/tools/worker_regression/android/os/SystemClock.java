package android.os;
public final class SystemClock {
    public static long time;
    private SystemClock() {}
    public static long elapsedRealtime() { return time; }
}
