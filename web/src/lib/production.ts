// Whether an environment's name suggests production: the form's starting point, never the answer.
// The same rule as looks_like_production in src/sshfleet/config/schema.py; keep the two alike.
const NOT_PRODUCTION = /(pre|non)[-_ .]?prod/i;

/** `prod` anywhere (production, prodEnv, eu1prod), or `prd` or `live` as a word (liveEnv, eu1prd),
 *  but not preprod or non-prod. `main`, or a name in another language, is the operator's to mark. */
export function looksLikeProduction(name: string): boolean {
  if (NOT_PRODUCTION.test(name)) return false;
  const words = name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase().match(/[a-z]+|\d+/g) ?? [];
  return name.toLowerCase().includes("prod") || words.some((w) => w === "prd" || w === "live");
}
