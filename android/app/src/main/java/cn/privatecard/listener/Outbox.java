package cn.privatecard.listener;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import java.util.ArrayList;
import java.util.List;

final class Outbox extends SQLiteOpenHelper {
    private static Outbox instance;
    static synchronized Outbox get(Context context) {
        if (instance == null) instance = new Outbox(context.getApplicationContext());
        return instance;
    }
    private Outbox(Context context) { super(context, "receipt-outbox.db", null, 1); }
    @Override public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE events (id TEXT PRIMARY KEY, origin TEXT NOT NULL, body TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)");
        db.execSQL("CREATE INDEX events_pending ON events(delivered, next_attempt)");
    }
    @Override public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {}
    synchronized boolean enqueue(String id, String origin, String body) {
        SQLiteDatabase db = getWritableDatabase();
        try (Cursor cursor = db.rawQuery("SELECT 1 FROM events WHERE id=?", new String[]{id})) {
            if (cursor.moveToFirst()) return false;
        }
        if (pendingCount() >= 2000) throw new IllegalStateException("离线上报队列已满，请联网并检查服务器");
        ContentValues values = new ContentValues();
        values.put("id", id); values.put("origin", origin); values.put("body", body);
        values.put("created_at", System.currentTimeMillis());
        return db.insertWithOnConflict("events", null, values, SQLiteDatabase.CONFLICT_IGNORE) != -1;
    }
    synchronized List<Entry> due(String origin) {
        ArrayList<Entry> rows = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().rawQuery(
                "SELECT id, body, attempts FROM events WHERE delivered=0 AND next_attempt<=? AND origin=? ORDER BY created_at LIMIT 20",
                new String[]{Long.toString(System.currentTimeMillis()), origin})) {
            while (cursor.moveToNext()) rows.add(new Entry(cursor.getString(0), cursor.getString(1), cursor.getInt(2)));
        }
        return rows;
    }
    synchronized int pendingCount() {
        try (Cursor cursor = getReadableDatabase().rawQuery("SELECT COUNT(*) FROM events WHERE delivered=0", null)) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }
    synchronized void delivered(String id) {
        ContentValues values = new ContentValues();
        values.put("delivered", 1);
        values.put("body", ""); // Keep the deduplication ID, discard financial notification text.
        getWritableDatabase().update("events", values, "id=?", new String[]{id});
        getWritableDatabase().delete("events", "delivered=1 AND created_at<?",
                new String[]{Long.toString(System.currentTimeMillis() - 90L * 86400000L)});
    }
    synchronized void failed(Entry entry, boolean permanent) {
        long seconds = permanent ? 1800L : Math.min(1800L, 15L * (1L << Math.min(entry.attempts, 7)));
        ContentValues values = new ContentValues();
        values.put("attempts", entry.attempts + 1);
        values.put("next_attempt", System.currentTimeMillis() + seconds * 1000L);
        getWritableDatabase().update("events", values, "id=?", new String[]{entry.id});
    }
    synchronized void retryNow() {
        ContentValues values = new ContentValues(); values.put("next_attempt", 0);
        getWritableDatabase().update("events", values, "delivered=0", null);
    }
    static final class Entry {
        final String id; final String body; final int attempts;
        Entry(String id, String body, int attempts) { this.id = id; this.body = body; this.attempts = attempts; }
    }
}
