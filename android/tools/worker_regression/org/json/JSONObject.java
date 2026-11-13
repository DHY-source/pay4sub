package org.json;
import java.util.LinkedHashMap;
public final class JSONObject {
    private final LinkedHashMap<String, Object> values = new LinkedHashMap<>();
    public JSONObject() {}
    public JSONObject(String json) { throw new UnsupportedOperationException("Network JSON parsing is outside this deterministic fixture"); }
    public JSONObject put(String key, Object value) { values.put(key, value); return this; }
    public boolean optBoolean(String key, boolean fallback) {
        Object value = values.get(key); return value instanceof Boolean ? (Boolean) value : fallback;
    }
    public String optString(String key, String fallback) {
        Object value = values.get(key); return value instanceof String ? (String) value : fallback;
    }
    @Override public String toString() { return values.toString(); }
}
