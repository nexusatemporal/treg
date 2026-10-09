import { expect, test } from '@playwright/test'
import { json, signIn } from './helpers'

const GBP = 'google-business-profile'
const MESSAGE = 'Google Business Profile is temporarily paused on treg. Your existing connection is saved and starts to work again when it is resumed. No action is needed from you.'

// The shapes a deployment with TREG_PAUSED_PROVIDERS=google-business-profile answers with: the
// provider is out of the listing, and its kept connection is marked paused.
test('a paused provider loses its chip, and its connection shows paused instead of vanishing', async ({ page }) => {
  await page.route(url => url.pathname === '/oauth/providers', async route => {
    const listing = await (await route.fetch()).json()
    await route.fulfill(json(listing.filter((p: { service: string }) => p.service !== GBP)))
  })
  await page.route(url => url.pathname === '/connections', route => route.fulfill(json([{
    id: 41, name: GBP, kind: 'oauth', provider: GBP, authorization_method: 'default', resource_name: '',
    needs_reconnect: false, resource_ref: '', scopes: [], health: 'invalid', health_detail: 'HTTP 429',
    refreshable: true, expiry_state: 'ok', expires_at: null, last_refresh_at: null, last_error: '',
    owner: 'a@example.com', created_at: '2026-09-01T00:00:00', paused: true, paused_message: MESSAGE,
    provider_display_name: 'Google Business Profile',
  }])))
  await signIn(page)

  await page.goto('/app#connections')
  const card = page.locator('.cn-card').filter({ hasText: 'Google Business Profile' })
  await expect(card.locator('.cn-st')).toHaveText('Paused')
  await expect(card).toContainText(MESSAGE)
  await expect(card.getByRole('button', { name: 'Reconnect' })).toHaveCount(0)
  await expect(page.locator('#prov-' + GBP)).toHaveCount(0)

  await page.goto('/app#start')
  const chips = page.locator('.prov-chip')
  await expect(chips.filter({ hasText: 'Search Console' })).toBeVisible()
  await expect(chips.filter({ hasText: 'Business Profile' })).toHaveCount(0)
})
