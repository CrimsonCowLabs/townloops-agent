-- 030_event_image_age.sql
-- Event images and typed fields (#64).
--
-- image_alt: the alt text of `image_url`, copied from where the image was
-- found (og:image:alt, a JSON-LD ImageObject's caption, a listing card's
-- markdown alt, an adapter feed). TownLoops requires alt with any image and
-- an image is only sent with one (engine/images). NULL alt = no shippable
-- image, whatever `image_url` holds (event-reconcile's url-only image).
-- age_restriction: 'all_ages' | 'adults_only' from a quote-checked sentence
-- on the event's own page (event-read-detail); NULL = the page didn't say.

ALTER TABLE events ADD COLUMN image_alt TEXT;
ALTER TABLE events ADD COLUMN age_restriction TEXT;
