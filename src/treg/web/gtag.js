// Google Ads conversion tracking via gtag.js. Loaded on every public and authenticated page.
// The base tag sends pageviews; tregSignupConversion() fires the one-time signup event.
//
// Conversion ID: AW-18392771132
// Conversion action: treg Signup (web) (7745505287), 30-day click window (configured in Google Ads)
(function () {
  try {
    // Load the gtag.js script async (does not block page load)
    var s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=AW-18392771132';
    document.head.appendChild(s);

    // Initialize the data layer and gtag function
    window.dataLayer = window.dataLayer || [];
    function gtag() { dataLayer.push(arguments); }
    window.gtag = gtag;
    gtag('js', new Date());
    gtag('config', 'AW-18392771132');

    // Fire the signup conversion. Called by the dashboard on first team creation for a new user.
    // Uses localStorage to ensure it fires only once per browser, even if the signup flow retries.
    window.tregSignupConversion = function () {
      try {
        var key = 'treg_signup_conv_fired';
        if (localStorage.getItem(key)) return;
        gtag('event', 'conversion', {
          'send_to': 'AW-18392771132/0usqCIeQrO0cELzUrcJE',
          'value': 1.0,
          'currency': 'AUD'
        });
        localStorage.setItem(key, '1');
      } catch (e) { /* never break the page for a marketing event */ }
    };
  } catch (e) { /* never break the page for a marketing script */ }
})();
