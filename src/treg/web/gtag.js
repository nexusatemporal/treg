// Google Ads base tag via gtag.js. Loaded on marketing pages only (landing, use-case pages,
// resources, tutorial, etc.) — NOT the signed-in dashboard. The base tag sends pageviews to
// Google Ads for ad-click attribution. Signup conversions are tracked server-side via adsconv.py,
// not here, to avoid double-counting.
//
// Conversion ID: AW-18392771132
// See docs/context/architecture/ads-conversions.md for the full tracking architecture.
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
  } catch (e) { /* never break the page for a marketing script */ }
})();
