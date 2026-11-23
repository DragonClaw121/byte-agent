package io.byteagent.assistente;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Fica atento aos avisos do espaço da pessoa (lembretes, resumos, atas, e-mails...) e mostra como notificação.
 * Pergunta ao servidor a cada 60 s. Serviço em primeiro plano (notificação fixa discreta): exigência do Android.
 */
public class AvisosService extends Service {
    static final String CH_FIXO = "assistente-atento", CH_AVISOS = "assistente-avisos";
    static final int ID_FIXO = 1;
    private volatile boolean on = true;
    private Thread loop;

    static void start(Context c) {
        if (!Rede.connected(c) || !Rede.prefs(c).getBoolean("avisos", true)) return;
        Intent i = new Intent(c, AvisosService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
            else c.startService(i);
        } catch (Exception ignored) {
            // o Android pode recusar iniciar em segundo plano; abre de novo quando o app for aberto
        }
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, AvisosService.class));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        channels(this);
        Notification n = new Notification.Builder(this, CH_FIXO)
                .setSmallIcon(android.R.drawable.ic_popup_reminder)
                .setContentTitle("Atento aos seus avisos")
                .setContentText("Lembretes e avisos do seu assistente chegam aqui.")
                .setOngoing(true)
                .setContentIntent(open(this, 0))
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID_FIXO, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(ID_FIXO, n);
        }
        if (loop == null || !loop.isAlive()) {
            on = true;
            loop = new Thread(this::poll, "assistente-avisos");
            loop.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        on = false;
        super.onDestroy();
    }

    private void poll() {
        while (on) {
            if (Rede.connected(this)) {
                try {
                    JSONObject res = Rede.get(Rede.base(this) + "/api/avisos", Rede.token(this));
                    int status = res.optInt("_status", 0);
                    if (status == 401) {                      // link trocado ou acesso removido
                        notifyText(this, "O link do seu painel não vale mais. Abra o app e cole o link novo (peça /painel).");
                        stopSelf();
                        return;
                    }
                    JSONArray items = res.optJSONArray("avisos");
                    for (int i = 0; items != null && i < items.length(); i++) {
                        String text = items.getJSONObject(i).optString("text", "");
                        if (!text.isEmpty()) notifyText(this, text);
                    }
                } catch (Exception ignored) {
                    // sem rede agora: tenta na próxima volta
                }
            }
            for (int s = 0; s < 60 && on; s++) {
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }
    }

    static void channels(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CH_FIXO, "Atento aos avisos", NotificationManager.IMPORTANCE_MIN));
        nm.createNotificationChannel(new NotificationChannel(CH_AVISOS, "Avisos do assistente", NotificationManager.IMPORTANCE_HIGH));
    }

    static PendingIntent open(Context c, int code) {
        return PendingIntent.getActivity(c, code, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
    }

    static void notifyText(Context c, String text) {
        channels(c);
        String title = Rede.prefs(c).getString("agente", "");
        String preview = text.replace('\n', ' ').trim();
        if (preview.length() > 120) preview = preview.substring(0, 117) + "...";
        Notification n = new Notification.Builder(c, CH_AVISOS)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title.isEmpty() ? "Meu Assistente" : title)
                .setContentText(preview)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setAutoCancel(true)
                .setContentIntent(open(c, 2))
                .build();
        c.getSystemService(NotificationManager.class).notify((int) (System.currentTimeMillis() % 100000) + 10, n);
    }
}
