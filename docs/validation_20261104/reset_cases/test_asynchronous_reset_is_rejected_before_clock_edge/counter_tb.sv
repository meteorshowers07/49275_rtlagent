`timescale 1ns/1ps
module counter_tb;
  logic clk=0, rst=0, en=0;
  wire [3:0] q;
  logic [3:0] expected=0;
  integer i;
  counter dut(.clk(clk), .rst(rst), .en(en), .q(q));
  always #2.5 clk=~clk;
  initial begin
    // Establish known, nonzero state before testing off-edge reset behavior.
    #1; rst=1;
    @(posedge clk); #0.1;
    if (q !== 4'd0) $fatal(1, "reset must set q to zero even with en low");
    @(negedge clk); rst=0; en=1;
    @(posedge clk); #0.1;
    if (q !== 4'd1) $fatal(1, "increment after reset failed");
    @(negedge clk); en=0;
    #0.5; rst=1;
    #0.1;
    if (q !== 4'd1) $fatal(1, "reset is asynchronous: q changed before a clock edge; use only posedge clk in the event control");
    // A pulse wholly between clock edges must have no effect either.
    #0.5; rst=0;
    @(posedge clk); #0.1;
    if (q !== 4'd1) $fatal(1, "synchronous reset pulse between edges must not change q");
    @(negedge clk); #0.5; rst=1;
    #0.1;
    if (q !== 4'd1) $fatal(1, "synchronous reset must wait for posedge clk");
    @(posedge clk); #0.1;
    if (q !== 4'd0) $fatal(1, "active-high reset must clear q at posedge clk with priority over enable");
    for (i=0; i<80; i=i+1) begin
      @(negedge clk);
      rst = (i==0 || i==31 || i==32);
      en = (i % 7 != 0);
      @(posedge clk);
      if (rst) expected=0;
      else if (en) expected=expected+4'd1;
      #0.1;
      if (q !== expected) $fatal(1, "mismatch cycle=%0d q=%0d expected=%0d", i, q, expected);
    end
    $display("RTL_TEST_PASS:counter");
    $finish;
  end
  initial begin #1000; $fatal(1, "watchdog timeout"); end
endmodule
