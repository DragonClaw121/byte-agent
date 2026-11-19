package io.byteagent.kronus;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Celular reiniciou (ou o app foi atualizado): volta a ficar atento aos avisos, se a opção estiver ligada. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        if (c.getSharedPreferences("kronus", Context.MODE_PRIVATE).getBoolean("ligar", false)) {
            try {
                KronusWatchService.start(c);
            } catch (Exception ignored) {
                // alguns celulares só deixam religar quando o app for aberto
            }
        }
    }
}
