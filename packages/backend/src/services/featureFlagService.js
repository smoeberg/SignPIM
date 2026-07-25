class FeatureFlagService {
  constructor(config = {}) {
    this.config = config;
  }

  isEnabled(flagName, tenantId) {
    const flag = this.config[flagName];
    if (!flag) return false;
    
    if (flag.tenantIds && !flag.tenantIds.includes(tenantId)) return false;
    if (flag.percentage && Math.random() > flag.percentage) return false;
    
    return flag.defaultValue || false;
  }
}

module.exports = { FeatureFlagService };
