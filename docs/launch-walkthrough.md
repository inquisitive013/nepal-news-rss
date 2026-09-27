# Nepal Wire launch walkthrough

The order matters. Each step feeds the next. Nothing here needs code: every value you collect goes into `config/settings.yaml`, and I apply it the moment you send it. Never paste a token or key into chat, a commit or this file. Secrets go only into *Settings → Secrets and variables → Actions*.

## 0. Two Facebook fixes first. Five minutes.

1. **The Page ID secret is stale.** Open the Page, *About → Page transparency*, copy the Page ID. In the repository open *Settings → Secrets and variables → Actions*, edit `FACEBOOK_PAGE_ID`, paste, save. The newsroom tolerates the stale value only while the token manages exactly one Page.
2. **The token is a user token.** It dies in about 60 days and every post stops with it. In *Graph API Explorer* call `me/accounts` with the extended token. Copy the `access_token` shown for Nepal Wire, not the one at the top. Replace the `FACEBOOK_PAGE_TOKEN` secret with it. That token does not expire. Then run *Actions → Check social accounts*: the note about a derived token disappears.

## 1. GitHub: paid plan, private repository, two factor

1. github.com → *Settings → Billing and plans → Plans and usage*. It must say GitHub Pro or higher. The Free plan cannot serve GitHub Pages from a private repository.
2. Repository → *Settings → General*, scroll to *Danger Zone → Change repository visibility → Make private*. Type the repository name to confirm. Actions, secrets, Pages and the daily schedule keep running. The site keeps its address.
3. *Settings → Password and authentication* → turn on two factor with an authenticator app. Your GitHub login controls the newsroom, the Facebook token and the site. Protect it like a bank login.
4. Run *Actions → Daily edition → Run workflow* with **rebuild_only** ticked. When it finishes, open the site. If it loads, the private repository serves Pages correctly.
5. Private repositories spend the plan's included Actions minutes. After a week open *Settings → Billing → Usage* once and look at the count. One edition a day stays well inside a Pro plan.

Every link to the code and the issue tracker already left the site. Readers report errors by email once step 3 is done, through the Facebook Page until then.

## 2. The domain

My pick is nepalwire.com, then nepalwire.news, then nepalwire.org. I cannot check availability from here. You buy, I wire it up.

1. Buy at Cloudflare Registrar (sells at cost, DNS included) or any registrar. Turn on privacy for the owner record.
2. Add these DNS records at the registrar:

| Type | Name | Value |
|---|---|---|
| A | @ | 185.199.108.153 |
| A | @ | 185.199.109.153 |
| A | @ | 185.199.110.153 |
| A | @ | 185.199.111.153 |
| CNAME | www | inquisitive013.github.io |

   On Cloudflare set every record to *DNS only* (grey cloud), not proxied. A proxied record breaks the certificate GitHub issues.
3. Repository → *Settings → Pages → Custom domain*: enter the bare domain, save. GitHub checks the DNS. When *Enforce HTTPS* becomes clickable, tick it. That can take up to an hour after the records go live.
4. Send me the domain. I set `site.custom_domain` and merge. The next build moves every link, both feeds, both sitemaps and the CNAME file to it. The old github.io address redirects.
5. Check: the domain loads with the padlock, the Nepali front loads at `/ne/`, the github.io address forwards.

## 3. Google Workspace: the domain and sponsors@

You already pay for Workspace. A group address costs nothing extra. A second user licence does, so use groups.

1. admin.google.com → *Account → Domains → Manage domains → Add a domain*. Choose **Secondary domain** so the newsroom gets its own addresses. Enter the domain.
2. Google shows a TXT record for verification. Add it at the registrar. Click *Verify*. Minutes to an hour.
3. Mail: add the MX record Google shows. The current one is a single record, `smtp.google.com`, priority 1.
4. *Directory → Groups → Create group*. Email `sponsors@` on the new domain. Add yourself as a member. Under access, allow *Anyone on the web* to post, or outside sponsors cannot write to it. Make it a *Collaborative inbox* so replies stay threaded. Repeat for `desk@` if you want a corrections address separate from sales.
5. Send a test from your Gmail to the new address. When it lands, send me the address. I set `site.contact_email`. The sponsor page, the standards page and every footer switch to it.
6. Outgoing mail from those addresses needs three more TXT records, or sponsors' filters junk it. Google's wizard shows the exact values: SPF (`v=spf1 include:_spf.google.com ~all`), DKIM (*Apps → Google Workspace → Gmail → Authenticate email → Generate new record*), DMARC (`_dmarc`, start with `p=none`).

## 4. Beehiiv: the newsletter and the members tier

One decision first. Beehiiv's paid subscriptions run on Stripe, and Stripe does not onboard businesses based in Nepal. If you have a company or residence in a country Stripe serves, memberships work as designed. If not, launch with the free newsletter, sponsorships and ads, and tell me where the money should land so I can design the members tier around a route that works (bank transfer, Payoneer, or a local wallet with a manual list).

1. beehiiv.com → create a publication named Nepal Wire. Upload the same logo. Description: the tagline.
2. Copy the subscribe page link. Send it to me. I set `newsletter.signup_url`. Every sign up box on the site turns into a button.
3. If you prefer the form inline, create a subscribe form in Beehiiv, copy its embed HTML, send it. I set `newsletter.embed_html`.
4. Paid tier, only if Stripe works for you: *Monetization → Subscriptions*, connect Stripe, create one tier, name it Member, price it for the diaspora (USD, monthly and yearly). Copy the upgrade link. I set `newsletter.members_url`.
5. The daily email is manual until we automate it: the three stories, each with its take and the "What the coverage missed" line, one paragraph each. Ask me for a generator when the list passes a few hundred readers.

## 5. Google Search Console

1. search.google.com/search-console → *Add property* → **Domain** → enter the bare domain. Google shows a TXT record. Add it at the registrar. Verify. A domain property covers www, the bare domain and `/ne/` together.
2. *Sitemaps* → add `sitemap.xml`, then `news-sitemap.xml`.
3. No token is needed with a domain property. If you also add a URL prefix property, Google offers an HTML tag with a token. Send it and I set `site.google_site_verification`.

## 6. Google AdSense

AdSense wants a domain you own with content on it, so this waits for step 2.

1. adsense.google.com → sign up with the Workspace account → enter the domain.
2. *Account → Account information* shows the publisher ID, `ca-pub-` and sixteen digits. Send it. I set `ads.adsense_client`. The next build loads the script on every page, places two units on every article and writes `ads.txt`. AdSense reads that file to confirm ownership.
3. Google then reviews the site. Days to a few weeks. The privacy page it asks for exists.
4. *Payments → Payment methods* lists what works for your country. Set it up when the review passes.

## 7. The Facebook Page

1. Page → *About* → add the website address once the domain is live.
2. Send me the Page's address (the vanity URL, or facebook.com followed by the Page ID). I set `site.facebook_url`. The footer gets a Facebook link and the Page becomes the corrections channel wherever no email is set.

## 8. The Nepali edition: backfill the live stories

The four stories already published have no Nepali version. *Actions → Daily edition → Run workflow*, tick **backfill_translations**, run. It translates each story, checks the translation, commits and deploys. Two or three model calls per story. No new edition, no posts. From the next edition on, every story arrives in both languages and the Facebook caption opens in Nepali.

## What I set for you, and where

| You send | I set | Effect |
|---|---|---|
| the domain | `site.custom_domain` | every link, both feeds, both sitemaps, CNAME |
| sponsors@ address | `site.contact_email` | sponsor page, standards, footers, corrections channel |
| Beehiiv subscribe link | `newsletter.signup_url` | sign up buttons everywhere |
| Beehiiv upgrade link | `newsletter.members_url` | member links on the front and investigations pages |
| AdSense publisher ID | `ads.adsense_client` | script, two units per article, ads.txt |
| Facebook Page address | `site.facebook_url` | footer link, corrections fallback |
| Search Console tag token | `site.google_site_verification` | only for a URL prefix property |
