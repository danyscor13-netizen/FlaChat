/* =========================================================
   FlaChat — service worker
   Gira in background, anche a scheda chiusa: è il motivo per cui
   le notifiche arrivano quando non stai guardando il sito.
   ========================================================= */

self.addEventListener('install', function (e) {
    // attiva subito la nuova versione invece di aspettare
    // che tutte le schede vecchie si chiudano
    self.skipWaiting();
});

self.addEventListener('activate', function (e) {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (e) {
    var d = {};
    try {
        d = e.data ? e.data.json() : {};
    } catch (err) {
        d = { title: 'FlaChat', body: e.data ? e.data.text() : '' };
    }

    e.waitUntil(
        self.registration.showNotification(d.title || 'FlaChat', {
            body: d.body || '',
            // stesso tag = le notifiche dello stesso canale si
            // sostituiscono invece di impilarsi a decine
            tag: d.tag || 'flachat',
            renotify: true,
            data: { url: d.url || '/' }
        })
    );
});

self.addEventListener('notificationclick', function (e) {
    e.notification.close();
    var url = (e.notification.data && e.notification.data.url) || '/';

    // se FlaChat è già aperto in una scheda, portala in primo piano
    // invece di aprirne una nuova
    e.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true })
            .then(function (lista) {
                for (var i = 0; i < lista.length; i++) {
                    if (lista[i].url.indexOf(url) !== -1 && 'focus' in lista[i]) {
                        return lista[i].focus();
                    }
                }
                if (self.clients.openWindow) {
                    return self.clients.openWindow(url);
                }
            })
    );
});
