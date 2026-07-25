class TaskEngine {
  constructor() {
    this.tasks = [];
  }

  createTask(productId, ruleName, priority = 'high') {
    const task = {
      id: `task_${Date.now()}`,
      productId,
      ruleName,
      status: 'review_required',
      priority,
      createdAt: new Date().toISOString()
    };
    this.tasks.push(task);
    return task;
  }

  approveTask(taskId, userId) {
    const task = this.tasks.find(t => t.id === taskId);
    if (task) {
      task.status = 'approved';
      task.approvedBy = userId;
    }
    return task;
  }
}

module.exports = { TaskEngine };
