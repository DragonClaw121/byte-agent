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
            hangUp();
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
