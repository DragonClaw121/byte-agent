package io.byteagent.kronus;

import android.Manifest;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.location.Location;
import android.location.LocationManager;
import android.os.Build;
import android.os.CancellationSignal;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * v1.13: "📍 Compartilhar minha localização". Manda ao Kronus a posição APROXIMADA (rede/Wi-Fi, ~1 km), para o clima e
 * as sugestões seguirem você. Só com a opção ligada e a permissão dada; o servidor guarda só a última posição.
 */
final class Local {
    static final String PREF = "localizacao";
    static final long EVERY_MS = 15 * 60 * 1000L;
    private static volatile long lastSent = 0;

    private Local() { }

    static boolean allowed(Context c) {
        return c.getSharedPreferences("kronus", Context.MODE_PRIVATE).getBoolean(PREF, false)
                && c.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    /** Envia se estiver ligado e se já passou o intervalo (force = agora). Chame fora da thread da tela. */
    static String sendIfDue(Context c, boolean force) {
        if (!allowed(c)) return "";
        if (!force && System.currentTimeMillis() - lastSent < EVERY_MS) return "";
        SharedPreferences prefs = c.getSharedPreferences("kronus", Context.MODE_PRIVATE);
        String server = prefs.getString("server", ""), token = prefs.getString("token", "");
        if (server.isEmpty() || token.isEmpty()) return "";
        Location loc = locate(c);
        if (loc == null) return "⚠️ Localização indisponível (ative a localização do celular).";
        try {
            JSONObject body = new JSONObject();
            body.put("lat", loc.getLatitude());
            body.put("lon", loc.getLongitude());
            body.put("accuracy", (int) loc.getAccuracy());
            JSONObject res = Voz.http(server + "/api/v1/location", "application/json",
                    body.toString().getBytes(StandardCharsets.UTF_8), token);
            lastSent = System.currentTimeMillis();
            return res.optBoolean("known") ? "📍 " + res.optString("onde") : "";
        } catch (Exception e) {
            return "";
        }
    }

    @SuppressWarnings("MissingPermission")
    static Location locate(Context c) {
        if (c.checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) != PackageManager.PERMISSION_GRANTED) return null;
        LocationManager lm = c.getSystemService(LocationManager.class);
        if (lm == null) return null;
        List<String> providers = new ArrayList<>();
        if (Build.VERSION.SDK_INT >= 31) providers.add(LocationManager.FUSED_PROVIDER);
        providers.add(LocationManager.NETWORK_PROVIDER);
        providers.add(LocationManager.PASSIVE_PROVIDER);
        if (Build.VERSION.SDK_INT >= 30) {
            for (String p : providers) {
                try {
                    if (!lm.isProviderEnabled(p) && !LocationManager.PASSIVE_PROVIDER.equals(p)) continue;
                    CountDownLatch latch = new CountDownLatch(1);
                    final Location[] got = {null};
                    CancellationSignal cancel = new CancellationSignal();
                    lm.getCurrentLocation(p, cancel, Runnable::run, l -> { got[0] = l; latch.countDown(); });
                    if (!latch.await(20, TimeUnit.SECONDS)) cancel.cancel();
                    if (got[0] != null) return got[0];
                } catch (Exception ignored) {
                    // provedor indisponível com permissão aproximada: tenta o próximo
                }
            }
        }
        Location best = null;
        for (String p : providers) {
            try {
                Location l = lm.getLastKnownLocation(p);
                if (l != null && (best == null || l.getTime() > best.getTime())) best = l;
            } catch (Exception ignored) {
            }
        }
        return best;
    }
}
