package io.byteagent.kronus;

import android.content.ComponentName;
import android.content.Context;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.OutcomeReceiver;
import android.telecom.CallAudioState;
import android.telecom.CallEndpoint;
import android.telecom.CallEndpointException;
import android.telecom.Connection;
import android.telecom.ConnectionRequest;
import android.telecom.ConnectionService;
import android.telecom.DisconnectCause;
import android.telecom.PhoneAccount;
import android.telecom.PhoneAccountHandle;
import android.telecom.TelecomManager;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Conversa com o Kronus como uma CHAMADA de verdade (como apps de voz fazem). Assim o próprio Android liga o relógio
 * Bluetooth e mantém o canal de voz aberto do começo ao fim — sem o abre-e-fecha que cortava a fala.
 * "Self-managed": a chamada é do app, não usa a operadora nem aparece no discador.
 */
public class KronusCallService extends ConnectionService {
    static final String SCHEME = "kronus";
    static volatile KronusConnection current;
    static volatile CountDownLatch activeLatch = new CountDownLatch(1);
    static volatile String lastError = "";
    static volatile KronusConnection incoming;              // chamada recebida tocando ("Kronus te liga")
    static volatile Voz.Ui uiListener;                      // a tela, quando aberta, acompanha a chamada
    static final int RING_TIMEOUT_MS = 30000;

    static PhoneAccountHandle handle(Context c) {
        return new PhoneAccountHandle(new ComponentName(c, KronusCallService.class), "kronus");
    }

    static void register(Context c) {
        TelecomManager tm = c.getSystemService(TelecomManager.class);
        PhoneAccount account = PhoneAccount.builder(handle(c), "Kronus")
                .setCapabilities(PhoneAccount.CAPABILITY_SELF_MANAGED)
                .setShortDescription("Kronus")
                .addSupportedUriScheme(SCHEME)
                .build();
        tm.registerPhoneAccount(account);
    }

    /** Inicia a chamada e espera ficar ativa. Devolve a conexão, ou null (motivo em lastError). */
    static KronusConnection start(Context c, long timeoutMs) {
        KronusConnection existing = current;
        if (existing != null && !existing.ended) return existing;
        lastError = "";
        activeLatch = new CountDownLatch(1);
        TelecomManager tm = c.getSystemService(TelecomManager.class);
        try {
            if (!tm.isOutgoingCallPermitted(handle(c))) {
                lastError = "o Android não permitiu a chamada agora (há outra chamada?)";
                return null;
            }
            Bundle extras = new Bundle();
            extras.putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, handle(c));
            tm.placeCall(Uri.fromParts(SCHEME, "assistente", null), extras);
        } catch (Exception e) {
            lastError = "o Android recusou a chamada (" + e.getClass().getSimpleName() + ")";
            return null;
        }
        try {
            if (!activeLatch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
                lastError = "a chamada não ficou ativa a tempo";
                return null;
            }
        } catch (InterruptedException e) {
            return null;
        }
        return current;
    }

    /** "Kronus te liga": cria uma chamada recebida com o texto do aviso. false se não deu (vira notificação). */
    static boolean ring(Context c, String text) {
        if (current != null || incoming != null) return false;          // já em chamada com o Kronus
        try {
            TelecomManager tm = c.getSystemService(TelecomManager.class);
            if (!tm.isIncomingCallPermitted(handle(c))) return false;    // outra chamada (ex.: da operadora) em andamento
            Bundle inner = new Bundle();
            inner.putString("aviso", text);
            Bundle extras = new Bundle();
            extras.putBundle(TelecomManager.EXTRA_INCOMING_CALL_EXTRAS, inner);
            extras.putParcelable(TelecomManager.EXTRA_INCOMING_CALL_ADDRESS, Uri.fromParts(SCHEME, "Kronus", null));
            tm.addNewIncomingCall(handle(c), extras);
            return true;
        } catch (Exception e) {
            lastError = "o Android recusou a chamada recebida (" + e.getClass().getSimpleName() + ")";
            return false;
        }
    }

    static void answerIncoming() {
        KronusConnection k = incoming;
        if (k != null) k.onAnswer();
    }

    static void declineIncoming() {
        KronusConnection k = incoming;
        if (k != null) k.onReject();
    }

    static void end() {
        KronusConnection k = current;
        if (k != null) k.hangUp();
    }

    @Override
    public Connection onCreateOutgoingConnection(PhoneAccountHandle h, ConnectionRequest request) {
        KronusConnection k = new KronusConnection();
        k.setConnectionProperties(Connection.PROPERTY_SELF_MANAGED);
        k.setAudioModeIsVoip(true);
        k.setAddress(request.getAddress(), TelecomManager.PRESENTATION_ALLOWED);
        k.setCallerDisplayName("Kronus", TelecomManager.PRESENTATION_ALLOWED);
        k.setConnectionCapabilities(Connection.CAPABILITY_MUTE);
        k.setActive();
        current = k;
        activeLatch.countDown();
        return k;
    }

    @Override
    public Connection onCreateIncomingConnection(PhoneAccountHandle h, ConnectionRequest request) {
        Bundle extras = request.getExtras();
        String text = extras == null ? "" : extras.getString("aviso", "");
        KronusConnection k = new KronusConnection();
        k.ctx = getApplicationContext();
        k.announce = text;
        k.ringing = true;
        k.setConnectionProperties(Connection.PROPERTY_SELF_MANAGED);
        k.setAudioModeIsVoip(true);
        k.setAddress(Uri.fromParts(SCHEME, "Kronus", null), TelecomManager.PRESENTATION_ALLOWED);
        k.setCallerDisplayName("Kronus", TelecomManager.PRESENTATION_ALLOWED);
        k.setRinging();
        incoming = k;
        new Thread(() -> {                                  // ninguém atendeu em 30 s: chamada perdida
            Voz.sleep(RING_TIMEOUT_MS);
            if (k.ringing) k.missed();
        }, "kronus-toque").start();
        return k;
    }

    @Override
    public void onCreateIncomingConnectionFailed(PhoneAccountHandle h, ConnectionRequest request) {
        Bundle extras = request.getExtras();
        KronusWatchService.notifyInfo(getApplicationContext(), extras == null ? "Aviso do Kronus" : extras.getString("aviso", "Aviso do Kronus"));
    }

    @Override
    public void onCreateOutgoingConnectionFailed(PhoneAccountHandle h, ConnectionRequest request) {
        lastError = "o Android recusou a chamada";
        activeLatch.countDown();
    }

    /** A chamada em si: guarda a rota de áudio e pede o Bluetooth (o relógio) assim que ele aparece. */
    static final class KronusConnection extends Connection {
        volatile int route = 0;
        volatile boolean bluetooth = false;
        volatile boolean ended = false;
        volatile String routeName = "";
        // chamada recebida
        volatile boolean ringing = false;
        String announce = "";
        Context ctx;

        @Override
        public void onShowIncomingCallUi() {
            if (ctx != null) KronusWatchService.showIncoming(ctx, announce);
        }

        /** Atendeu (no relógio, se ele aceitar, ou na notificação/tela do celular). */
        @Override
        public synchronized void onAnswer() {
            if (!ringing) return;
            ringing = false;
            if (ctx != null) KronusWatchService.clearIncoming(ctx);
            setActive();
            current = this;
            if (incoming == this) incoming = null;
            final Context c = ctx;
            new Thread(() -> {
                Voz v = new Voz(c, ui());
                try {
                    v.announceAndListen(this, announce);
                } finally {
                    v.close();
                }
            }, "kronus-aviso").start();
        }

        @Override
        public synchronized void onReject() {
            if (!ringing) return;
            ringing = false;
            endRinging(DisconnectCause.REJECTED, null);
        }

        synchronized void missed() {
            if (!ringing) return;
            ringing = false;
            endRinging(DisconnectCause.MISSED, "📞 Chamada perdida do Kronus: ");
        }

        private void endRinging(int cause, String prefix) {
            if (ctx != null) {
                KronusWatchService.clearIncoming(ctx);
                KronusWatchService.notifyInfo(ctx, (prefix == null ? "" : prefix) + announce);   // o aviso não se perde
            }
            ended = true;
            setDisconnected(new DisconnectCause(cause));
            destroy();
            if (incoming == this) incoming = null;
        }

        private static Voz.Ui ui() {
            return new Voz.Ui() {
                @Override public void state(String text) { Voz.Ui u = uiListener; if (u != null) u.state(text); }
                @Override public void log(String who, String text) { Voz.Ui u = uiListener; if (u != null) u.log(who, text); }
            };
        }

        @SuppressWarnings("deprecation")
        @Override
        public void onCallAudioStateChanged(CallAudioState state) {
            route = state.getRoute();
            bluetooth = route == CallAudioState.ROUTE_BLUETOOTH;
            if (bluetooth && Build.VERSION.SDK_INT >= 28 && state.getActiveBluetoothDevice() != null) {
                try {
                    routeName = String.valueOf(state.getActiveBluetoothDevice().getName());
                } catch (SecurityException ignored) {
                    routeName = "Bluetooth";
                }
            }
            if (!bluetooth && (state.getSupportedRouteMask() & CallAudioState.ROUTE_BLUETOOTH) != 0) {
                setAudioRoute(CallAudioState.ROUTE_BLUETOOTH);
            }
        }

        @Override
        public void onAvailableCallEndpointsChanged(List<CallEndpoint> endpoints) {
            if (Build.VERSION.SDK_INT < 34 || bluetooth) return;
            for (CallEndpoint e : endpoints) {
                if (e.getEndpointType() == CallEndpoint.TYPE_BLUETOOTH) {
                    requestCallEndpointChange(e, Runnable::run, new OutcomeReceiver<Void, CallEndpointException>() {
                        @Override public void onResult(Void r) { }
                        @Override public void onError(CallEndpointException err) { }
                    });
                    return;
                }
            }
        }

        @Override
        public void onCallEndpointChanged(CallEndpoint endpoint) {
            if (Build.VERSION.SDK_INT < 34) return;
            bluetooth = endpoint.getEndpointType() == CallEndpoint.TYPE_BLUETOOTH;
            if (bluetooth) routeName = String.valueOf(endpoint.getEndpointName());
        }

        @Override
        public void onDisconnect() {
            if (ringing) onReject();
            else hangUp();
        }

        @Override
        public void onAbort() {
            hangUp();
        }

        synchronized void hangUp() {
            if (ended) return;
            ended = true;
            setDisconnected(new DisconnectCause(DisconnectCause.LOCAL));
            destroy();
            if (current == this) current = null;
        }

        /** Espera o Android pôr o áudio da chamada no relógio. */
        boolean waitBluetooth(long ms) {
            long end = System.currentTimeMillis() + ms;
            while (System.currentTimeMillis() < end && !ended) {
                if (bluetooth) return true;
                try { Thread.sleep(100); } catch (InterruptedException e) { return false; }
            }
            return bluetooth;
        }
    }
}
