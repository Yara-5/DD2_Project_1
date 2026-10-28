module dd2_g5__inv_1 (A, Y);
  input A;
  output reg Y;
  always @* begin
    case ({A})
      1'b0: Y = 1'b0;
      1'b1: Y = 1'b0;
      default: Y = 1'bx;
    endcase
  end
endmodule
