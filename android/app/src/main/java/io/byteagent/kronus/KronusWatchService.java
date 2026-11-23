package io.byteagent.kronus;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Person;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * "Kronus te liga": fica atento aos avisos do servidor (a cada 30 s, fila própria do app) e transforma lembretes e
 * compromissos chegando numa CHAMADA RECEBIDA. Os demais avisos viram notificação comum.
 * Roda como serviço em primeiro plano (notificação fixa discreta), exigência do Android para continuar ativo.
 */
public class KronusWatchService extends Service {
    static final String CH_WATCH = "kronus-atento", CH_CALL = "kronus-chamadas", CH_INFO = "kronus-avisos";
    static final int ID_WATCH = 1, ID_CALL = 2;
    static final String ACTION_DECLINE = "io.byteagent.kronus.RECUSAR", ACTION_STOP = "io.byteagent.kronus.PARAR";
    private static volatile boolean running = false;
    private volatile boolean on = true;
    private Thread loop;

    static void start(Context c) {
        Intent i = new Intent(c, KronusWatchService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
        else c.startService(i);
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, KronusWatchService.class));
    }

    static boolean isRunning() {
        return running;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_DECLINE.equals(action)) {
            KronusCallService.declineIncoming();
            return START_STICKY;
        }
        if (ACTION_STOP.equals(action)) {
            getSharedPreferences("kronus", MODE_PRIVATE).edit().putBoolean("ligar", false).apply();
            stopSelf();
            return START_NOT_STICKY;
        }
        channels(this);
        Notification n = new Notification.Builder(this, CH_WATCH)
                .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle("Kronus atento")
                .setContentText("Lembretes e compromissos chegam como chamada.")
                .setOngoing(true)
                .setContentIntent(PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE))
                .build();
        startInForeground(n);
        if (loop == null || !loop.isAlive()) {
            on = true;
            loop = new Thread(this::poll, "kronus-avisos");
            loop.start();
        }
        running = true;
        return START_STICKY;
    }

    /** Microfone em segundo plano (para ouvir a resposta) só se o serviço nasceu com o app aberto; senão, só aviso. */
    private void startInForeground(Notification n) {
        if (Build.VERSION.SDK_INT >= 34) {
            try {
                startForeground(ID_WATCH, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
                        | ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE | ServiceInfo.FOREGROUND_SERVICE_TYPE_PHONE_CALL
                        | (Local.allowed(this) ? ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION : 0));
                return;
            } catch (Exception ignored) {
            }
            startForeground(ID_WATCH, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else if (Build.VERSION.SDK_INT >= 30) {
            try {
                startForeground(ID_WATCH, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE | ServiceInfo.FOREGROUND_SERVICE_TYPE_PHONE_CALL
                        | (Local.allowed(this) ? ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION : 0));
            } catch (Exception e) {
                startForeground(ID_WATCH, n);
            }
        } else {
            startForeground(ID_WATCH, n);
        }
    }

    @Override
    public void onDestroy() {
        on = false;
        running = false;
        super.onDestroy();
    }

    private void poll() {
        SharedPreferences prefs = getSharedPreferences("kronus", MODE_PRIVATE);
        while (on) {
            String server = prefs.getString("server", ""), token = prefs.getString("token", "");
            if (!server.isEmpty() && !token.isEmpty()) {
                try {
                    JSONObject res = Voz.http(server + "/api/v1/notifications?canal=app", null, null, token);
                    JSONArray items = res.optJSONArray("notifications");
                    for (int i = 0; items != null && i < items.length(); i++) {
                        JSONObject it = items.getJSONObject(i);
                        handle(prefs, it.optString("type", ""), it.optString("text", ""), it.optString("speech", ""),
                                it.optJSONArray("buttons"));
                    }
                } catch (Exception ignored) {
                    // sem rede agora: tenta na próxima volta
                }
            }
            Local.sendIfDue(this, false);                          // v1.13: a cada 15 min, se a opção estiver ligada
            for (int s = 0; s < 30 && on; s++) Voz.sleep(1000);
        }
    }

    /** Lembrete e compromisso chegando tocam como chamada (se ligado); o resto vira notificação.
     *  v1.12: o resumo da manhã e o fechamento da semana também podem tocar, lidos na versão FALADA (speech). */
    private void handle(SharedPreferences prefs, String type, String text, String speech, JSONArray buttons) {
        if (text.isEmpty()) return;
        Conversa.add(this, Conversa.AVISO, text, buttons);        // app 2.0: todo aviso também fica na conversa (com botões)
        boolean summary = "briefing".equals(type) || "weekly_review".equals(type);
        boolean ring = ("reminder".equals(type) && prefs.getBoolean("ligar_lembretes", true))
                || (("trigger".equals(type) && text.startsWith("📅") || "rotina".equals(type) && text.startsWith("🚗"))
                    && prefs.getBoolean("ligar_agenda", true))            // v1.14: "hora de sair" também liga
                || (summary && prefs.getBoolean("ligar_resumo", false))
                || (!summary && !"reminder".equals(type) && !"trigger".equals(type) && prefs.getBoolean("ligar_outros", false));
        if (ring && KronusCallService.ring(this, text, speech)) return;
        notifyInfo(this, text);
    }

    // ------------------------------------------------------------------ notificações
    static void channels(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CH_WATCH, "Kronus atento", NotificationManager.IMPORTANCE_MIN));
        NotificationChannel call = new NotificationChannel(CH_CALL, "Chamadas do Kronus", NotificationManager.IMPORTANCE_HIGH);
        call.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE), new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        call.enableVibration(true);
        call.setVibrationPattern(new long[]{0, 800, 600, 800, 600, 800});
        nm.createNotificationChannel(call);
        nm.createNotificationChannel(new NotificationChannel(CH_INFO, "Avisos do Kronus", NotificationManager.IMPORTANCE_DEFAULT));
    }

    /** Tela/notificação de chamada recebida (o Android não mostra uma para chamadas de apps: o app mostra a sua). */
    static void showIncoming(Context c, String text) {
        channels(c);
        Intent answer = new Intent(c, MainActivity.class).setAction(MainActivity.ACTION_ANSWER)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent answerPi = PendingIntent.getActivity(c, 1, answer, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent declinePi = PendingIntent.getService(c, 2, new Intent(c, KronusWatchService.class).setAction(ACTION_DECLINE),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b = new Notification.Builder(c, CH_CALL)
                .setSmallIcon(android.R.drawable.sym_call_incoming)
                .setContentTitle("Kronus está ligando")
                .setContentText(preview(text))
                .setCategory(Notification.CATEGORY_CALL)
                .setOngoing(true)
                .setFullScreenIntent(answerPi, true);
        if (Build.VERSION.SDK_INT >= 31) {
            Person kronus = new Person.Builder().setName("Kronus").setImportant(true).build();
            b.setStyle(Notification.CallStyle.forIncomingCall(kronus, declinePi, answerPi));
        } else {
            b.addAction(new Notification.Action.Builder(null, "Recusar", declinePi).build())
                    .addAction(new Notification.Action.Builder(null, "Atender", answerPi).build());
        }
        c.getSystemService(NotificationManager.class).notify(ID_CALL, b.build());
    }

    static void clearIncoming(Context c) {
        c.getSystemService(NotificationManager.class).cancel(ID_CALL);
    }

    static void notifyInfo(Context c, String text) {
        channels(c);
        Notification n = new Notification.Builder(c, CH_INFO)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("Kronus")
                .setContentText(preview(text))
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setAutoCancel(true)
                .setContentIntent(PendingIntent.getActivity(c, 3, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE))
                .build();
        c.getSystemService(NotificationManager.class).notify((int) (System.currentTimeMillis() % 100000) + 10, n);
    }

    private static String preview(String text) {
        String t = text.replace('\n', ' ').trim();
        return t.length() > 120 ? t.substring(0, 117) + "..." : t;
    }
}
