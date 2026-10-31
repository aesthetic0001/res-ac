export function curveSettings(value) {
  if (!value || typeof value !== 'object' || typeof value.enabled !== 'boolean' ||
      Object.keys(value).some(key => !['enabled', 'thresholdsC', 'hysteresisC', 'sessionId'].includes(key))) throw new Error('Invalid automatic curve settings');
  const { thresholdsC } = value;
  if (!Array.isArray(thresholdsC) || thresholdsC.length !== 5 || thresholdsC.some((temperature, index) =>
    typeof temperature !== 'number' || !Number.isFinite(temperature) || temperature < -20 || temperature > 60 ||
    (index > 0 && temperature < thresholdsC[index - 1]))) throw new Error('Use five nondecreasing temperatures from -20 to 60 °C; equal thresholds are allowed');
  const hysteresisC = value.hysteresisC === undefined ? 1 : value.hysteresisC;
  if (typeof hysteresisC !== 'number' || !Number.isFinite(hysteresisC) || hysteresisC < 0.5 || hysteresisC > 5) throw new Error('Hysteresis must be 0.5 to 5 °C');
  return { enabled: value.enabled, thresholdsC, hysteresisC };
}
