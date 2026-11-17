# Test corpora

Labelled (query, listing title) pairs the matcher is scored against. A label answers one
question as a buyer would: *is this listing a single, complete, working unit of the product
the query names, so that its price is a fair comparable?* Unpinned variants (a capacity,
colour, model-line qualifier or generation the query does not name) count as matches, since
the pricing stage prices them separately. `"ambiguous"` rows are excluded from scoring.

| File | Rows | What it is |
| --- | --- | --- |
| `match_corpus.json` | 454 | Synthetic titles written in eBay style across 22 queries, built to be hard: every accessory / part / damage / lot / replica / sibling-model trap. The development set. |
| `real_listings.json` | 1,469 | **Real** eBay.com listings for 15 product searches, each hand-labelled. The search for a query is every listing in the source crawl whose title contains all the query words. |
| `real_listings_holdout.json` | 963 | Real listings for 14 *other* products, labelled the same way and never used to tune a rule. The honest generalisation check. |

The real listings come from the public dataset
[`domserrea/ebay_productlisting`](https://huggingface.co/datasets/domserrea/ebay_productlisting)
on Hugging Face (a crawl of eBay.com from April 2021, published under a Creative Commons
licence). Only the title, price and used/new flag are kept, plus the labels added here.

Limits worth knowing: the labels are one labeller's judgement per row, not a consensus; the
crawl is US electronics from 2021, so it says nothing about other categories; and it has no
category, shipping, listing-date or sold data, so it exercises matching and price
stratification but not the lifecycle evidence.
