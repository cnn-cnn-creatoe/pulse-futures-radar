type Confirmation = { id: string; passed: boolean };
export const DEFAULT_STATISTICS_STRATEGY = 'wt_extreme_cross';
export function passesStatisticsStrategy(signal: {
  available_confirmations?: Confirmation[];
  confirmations?: Confirmation[];
}) {
  const confirmations = signal.available_confirmations?.length
    ? signal.available_confirmations : signal.confirmations || [];
  return confirmations.some(item => item.id === DEFAULT_STATISTICS_STRATEGY && item.passed === true);
}
