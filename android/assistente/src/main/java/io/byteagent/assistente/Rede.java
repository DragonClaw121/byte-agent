package io.byteagent.assistente;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Ligação com o agente: o link do painel pessoal (endereço + código) e pedidos HTTPS com o código. */
final class Rede {
    static final String PREFS = "assistente";

    private Rede() {
    }

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static String base(Context c) {
        return prefs(c).getString("base", "");
    }

    static String token(Context c) {
        return prefs(c).getString("token", "");
    }

    static boolean connected(Context c) {
        return !base(c).isEmpty() && !token(c).isEmpty();
    }

    /** "https://servidor/u/bit#CODIGO" -> {"https://servidor/u/bit", "CODIGO"}; null se não for um link de painel. */
    static String[] parse(String raw) {
        if (raw == null) return null;
        String text = raw.trim();
        int start = text.indexOf("https://");
        if (start < 0) return null;
        text = text.substring(start).split("\\s")[0];
        int hash = text.indexOf('#');
        if (hash < 0) return null;
        String base = text.substring(0, hash);
        while (base.endsWith("/")) base = base.substring(0, base.length() - 1);
        String token = text.substring(hash + 1).trim();
        if (!base.contains("/u/") || token.length() < 20 || !token.matches("[A-Za-z0-9_-]+")) return null;
        return new String[]{base, token};
    }

    static void save(Context c, String base, String token, String agent, String name) {
        prefs(c).edit().putString("base", base).putString("token", token).putString("agente", agent)
                .putString("nome", name).apply();
    }

    static void forget(Context c) {
        prefs(c).edit().remove("base").remove("token").remove("agente").remove("nome").apply();
    }

    /** GET com o código; devolve o JSON da resposta com "_status" (código HTTP). Lança exceção sem rede. */
    static JSONObject get(String url, String token) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setConnectTimeout(15000);
        conn.setReadTimeout(20000);
        conn.setRequestProperty("Authorization", "Bearer " + token);
        conn.setRequestProperty("Accept", "application/json");
        try {
            int code = conn.getResponseCode();
            InputStream in = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
            String body = "";
            if (in != null) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                body = new String(out.toByteArray(), StandardCharsets.UTF_8);
            }
            JSONObject json;
            try {
                json = new JSONObject(body.isEmpty() ? "{}" : body);
            } catch (Exception notJson) {
                json = new JSONObject();
            }
            json.put("_status", code);
            return json;
        } finally {
            conn.disconnect();
        }
    }
}
