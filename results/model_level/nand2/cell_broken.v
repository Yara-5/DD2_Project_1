module dd2_g5__nand2_1 (A, B, Y);
  input A;
  input B;
  output reg Y;
  always @* begin
    case ({A, B})
      2'b00: Y = 1'b0;
      2'b01: Y = 1'b1;
      2'b10: Y = 1'b1;
      2'b11: Y = 1'b0;
      default: Y = 1'bx;
    endcase
  end
endmodule
