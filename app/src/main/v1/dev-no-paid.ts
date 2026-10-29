// APP4：开发者开关"不付费试跑"。
// 只存在内存里，不写任何配置文件，每次启动都是关的；只有本次启动是开发者模式（开发版或 A2V_DEV_TOOLS=1）时才生效。
// 以前出过的问题：普通使用时出片被"这次运行设成了不花钱"拦住——开关不能影响普通使用。

export class DevNoPaid {
  private on = false

  constructor(private readonly devTools: boolean) {}

  // run / resume 是否带 --no-paid
  get enabled(): boolean { return this.devTools && this.on }

  set(value: unknown): boolean {
    this.on = this.devTools && value === true
    return this.enabled
  }
}
