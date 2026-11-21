package io.byteagent.kronus;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;

/**
 * App 2.0: o histórico da conversa com o Kronus, guardado no celular (últimas 200 mensagens).
 * Entram aqui: o que você escreve ou fala, as respostas, e os avisos que chegam (lembretes, resumos, rotina).
 */
final class Conversa {
    static final String EU = "eu", KRONUS = "kronus", AVISO = "aviso";
    private static final int MAX = 200;

    static final class Msg {
        String who, text;
        JSONArray buttons;
        long at;
        boolean used;
    }

    interface Listener {
        void changed();
    }

    static volatile Listener listener;
    private static List<Msg> cache;

    private Conversa() { }

    private static File file(Context c) {
        return new File(c.getFilesDir(), "conversa.json");
    }

    static synchronized List<Msg> all(Context c) {
        if (cache != null) return cache;
        cache = new ArrayList<>();
        try {
            File f = file(c);
            if (f.exists()) {
                JSONArray arr = new JSONArray(new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8));
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.getJSONObject(i);
                    Msg m = new Msg();
                    m.who = o.optString("who", KRONUS);
                    m.text = o.optString("text", "");
                    m.buttons = o.optJSONArray("buttons");
                    m.at = o.optLong("at");
                    m.used = o.optBoolean("used");
                    cache.add(m);
                }
            }
        } catch (Exception ignored) {
            // histórico corrompido: começa de novo
        }
        return cache;
    }

    static void add(Context c, String who, String text, JSONArray buttons) {
        if (text == null || text.trim().isEmpty()) {
            if (buttons == null || buttons.length() == 0) return;
        }
        synchronized (Conversa.class) {
            Msg m = new Msg();
            m.who = who;
            m.text = text == null ? "" : text.trim();
            m.buttons = buttons;
            m.at = System.currentTimeMillis();
            List<Msg> list = all(c);
            list.add(m);
            while (list.size() > MAX) list.remove(0);
            save(c);
        }
        Listener l = listener;
        if (l != null) l.changed();
    }

    static void markUsed(Context c, Msg m) {
        synchronized (Conversa.class) {
            m.used = true;
            save(c);
        }
        Listener l = listener;
        if (l != null) l.changed();
    }

    static void clear(Context c) {
        synchronized (Conversa.class) {
            all(c).clear();
            save(c);
        }
        Listener l = listener;
        if (l != null) l.changed();
    }

    private static void save(Context c) {
        try {
            JSONArray arr = new JSONArray();
            for (Msg m : all(c)) {
                JSONObject o = new JSONObject();
                o.put("who", m.who);
                o.put("text", m.text);
                if (m.buttons != null) o.put("buttons", m.buttons);
                o.put("at", m.at);
                if (m.used) o.put("used", true);
                arr.put(o);
            }
            File tmp = new File(c.getFilesDir(), "conversa.tmp");
            try (FileOutputStream out = new FileOutputStream(tmp)) {
                out.write(arr.toString().getBytes(StandardCharsets.UTF_8));
            }
            if (!tmp.renameTo(file(c))) {
                Files.copy(tmp.toPath(), file(c).toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (Exception ignored) {
        }
    }
}
